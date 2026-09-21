"""EduBuddy Desktop — application entry point.

Brings up a native WebView2 window. While `deeptutor start` boots, a splash
page shows live status; once 127.0.0.1:3782 answers, the window enters a
**login-gated session loop**:

    未登录  → 停留在登录门控页（WorkBuddy 风格：吉祥物 + 黑色登录按钮）
            → 点击按钮用系统浏览器打开 Tokengine 平台注册/登录（PKCE + 回环回调）
            → 登录成功：令牌 + 可用模型（按 model_type 分流）写入 DeepTutor
            → 导航进入应用
    已登录  → 直接进入应用
    应用内退出登录 → 吊销令牌、摘除模型配置 → 导航回登录门控页（功能不可用）
            → 再次登录后重新进入应用

Closing the window terminates the whole deeptutor process tree.

开发旁路：环境变量 ``DEEPTUTOR_DESKTOP_SKIP_LOGIN=1`` 跳过登录门控（离线开发用）。
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import threading
import time
import webbrowser
import ctypes
from collections import deque

from desktop import APP_NAME, __version__, clipboard, dialogs
from desktop.auth import AuthManager
from desktop.inject import LoginButtonInjector
from desktop.process import DeepTutorProcess, DEFAULT_FRONTEND_PORT
import desktop.runtime as rt
from desktop.splash import splash_html

log = logging.getLogger("dt.main")

# End users should never see the technical boot panel (ports / paths / logs).
# Set DEEPTUTOR_DESKTOP_DEBUG=1 to render it again for development.
DEBUG = os.environ.get("DEEPTUTOR_DESKTOP_DEBUG", "") == "1"

# --------------------------------------------------------------------------- #
# logging -------------------------------------------------------------------- #
LOG_DIR = rt.ROOT / "logs"
LOG_FILE = LOG_DIR / "app.log"


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    log.info("=== %s v%s starting ===", APP_NAME, __version__)


# --------------------------------------------------------------------------- #
# single instance ------------------------------------------------------------- #
def _single_instance() -> bool:
    """Return False if another instance already holds the lock."""
    lock = rt.ROOT / "app.lock"
    rt.ROOT.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            pid = int(lock.read_text().strip())
            os.kill(pid, 0)  # process alive -> still running
            return False
        except (ProcessLookupError, ValueError, OSError):
            lock.unlink(missing_ok=True)
            return _single_instance()


# --------------------------------------------------------------------------- #
# JS bridge / state ---------------------------------------------------------- #
class Api:
    """Methods callable from the splash/gate page via `pywebview.api.*`."""

    def __init__(self, frontend_url: str, auth: AuthManager, debug: bool = False) -> None:
        self._url = frontend_url
        self._auth = auth
        self._debug = debug
        self._lock = threading.Lock()
        self._phase = "boot"
        self._text = "正在初始化…"
        self._detail = ""
        self._loglines: deque[str] = deque(maxlen=5)

    def set_status(self, phase: str, text: str, detail: str = "") -> None:
        with self._lock:
            self._phase = phase
            self._text = text
            # technical detail is debug-only; end users just see the status line
            if detail and self._debug:
                self._detail = detail

    def push_line(self, line: str) -> None:
        """Feed a live boot log line. Ignored in production (debug-only)."""
        if not self._debug:
            return
        with self._lock:
            self._loglines.append(line)
            self._detail = "\n".join(self._loglines)

    # -- bridge methods called from the splash page -------------------- #
    def status(self) -> dict:
        with self._lock:
            return {
                "phase": self._phase,
                "text": self._text,
                "detail": self._detail,
            }

    def open_browser(self) -> None:
        webbrowser.open(self._url)

    # -- Tokengine 登录桥（登录门控页 / 应用内按钮共用）------------------ #
    def auth_status(self) -> dict:
        return self._auth.status()

    def login(self) -> dict:
        """发起 OAuth 登录；成功后令牌与模型自动写入 DeepTutor 并进入应用。"""
        result = self._auth.start_login()
        if result.get("ok"):
            webbrowser.open(result["url"])
            self.set_status("login", "已在浏览器打开 Tokengine 登录页…",
                            "请完成登录并在授权页点击「确认授权」")
        else:
            self.set_status("login", "无法发起登录",
                            result.get("detail") or result.get("error") or "")
        return result

    def logout(self) -> dict:
        return self._auth.logout()

    # -- 右键菜单动作（登录后）------------------------------------------ #
    def open_platform(self) -> dict:
        """系统浏览器打开 Tokengine 平台首页（注册/登录入口）。"""
        return self._auth.open_platform()

    def refresh_models(self) -> dict:
        """重拉 userinfo + /api/status → 重写 model_catalog → 回写账号信息。"""
        return self._auth.refresh_models()

    def copy_relay(self) -> dict:
        """复制 API 中继地址到剪贴板（刻意不复制业务 token，避免泄露）。"""
        st = self._auth.status()
        url = str(st.get("relay_base") or "").strip()
        if not url:
            return {"ok": False, "message": "暂无 API 地址可复制"}
        clipboard.set_text(url)
        return {"ok": True, "url": url}

    def about(self) -> dict:
        """『关于 EduBuddy』信息：壳版本 + deeptutor 版本 + 中继域名。"""
        return {
            "app": f"{APP_NAME} 桌面端",
            "app_version": __version__,
            "deeptutor_version": str(_shared.get("deeptutor_version") or "未知"),
            "relay": str(self._auth.status().get("relay_base") or ""),
        }

    def toast(self, msg: str) -> None:
        """在页面底部弹一条轻提示（右键菜单操作反馈用）。"""
        js = "window.__edubuddyToast(%s)" % json.dumps(str(msg), ensure_ascii=False)
        for w in webview_windows:
            try:
                w.evaluate_js(js)
                return
            except Exception:  # noqa: BLE001  窗口还没就绪 / 已关闭
                continue

    @staticmethod
    def quit() -> None:
        for w in webview_windows:
            try:
                w.destroy()
            except Exception:  # noqa: BLE001  (window already closed)
                pass


# shared handles ----------------------------------------------------
webview_windows: list = []
_shared: dict = {"proc": None}  # the live DeepTutorProcess


def _reload_page() -> None:
    """登录成功后刷新应用页面，让 DeepTutor 重新读取刚写入的模型目录。"""
    for w in webview_windows:
        try:
            w.evaluate_js("location.reload()")
            log.info("已刷新应用页面以载入新写入的模型")
            return
        except Exception:  # noqa: BLE001
            continue


# --------------------------------------------------------------------------- #
# 登录门控会话循环 ------------------------------------------------------------- #
def _gate_needed(auth: AuthManager) -> bool:
    """是否需要登录门控：显式旁路 > 本地可用令牌（登录写入的或手配的）。"""
    if os.environ.get("DEEPTUTOR_DESKTOP_SKIP_LOGIN", "") == "1":
        return False
    return not auth.has_usable_token()


def _wait_gate_login(api: Api, auth: AuthManager,
                     stop: threading.Event | None = None) -> bool:
    """停在登录门控页，阻塞直到一次登录尝试成功。

    页面文本经 ``api.set_status`` 推送（门控页每 300ms 轮询）：
    默认「登录后开始使用」；浏览器已打开显示等待文案；失败显示
    「登录未完成：<原因>」（门控页按前缀渲染成红字）。

    ``stop`` 置位时立即返回 False（窗口已关闭，调用方应退出会话循环），
    避免门控等待线程在窗口销毁后仍空转。
    """
    api.set_status("login", "登录后开始使用", "")
    while True:
        if stop is not None and stop.is_set():
            return False
        result = auth.wait_done(timeout=2.0)
        if result.get("ok"):
            return True
        err = result.get("error")
        if err == "timeout":
            continue          # 尚无尝试结束，继续等（按钮态由页面自轮询）
        if err == "skipped":
            # skip 通道已从 UI 移除；万一触发（旧页面残留）视为未登录继续等
            api.set_status("login", "登录后开始使用", "")
            continue
        msg = result.get("message") or "登录未完成"
        api.set_status("login", f"登录未完成：{msg}",
                       result.get("detail") or "")


def run_session(window, api: Api, url: str, auth: AuthManager,
                stop: threading.Event) -> None:
    """门控 ↔ 应用 的会话主循环（后台线程；窗口关闭时随进程退出）。

    每一轮：需要登录则停在门控页等成功 → 进入应用 → 等待退出登录信号
    → 导航回门控页（load_html 重载登录页，应用界面随之不可用）。

    退出登录后 ``force_gate`` 置位：下一轮**无条件**停在门控页等一次全新
    登录，不再复评 ``has_usable_token()``——注销后回门控是确定性动作，
    不能被 catalog 里残留的手配 api_key 等状态跳过（否则刚 load_html 的
    登录页会在几毫秒后被 load_url 覆盖，应用带着已吊销令牌闪回）。
    ``DEEPTUTOR_DESKTOP_SKIP_LOGIN=1`` 仅旁路启动门控，注销后仍回门控页。
    """
    logout_evt = threading.Event()
    _shared["logout_requested"] = logout_evt
    gate_page = splash_html(debug=DEBUG, version=__version__, gate=True)
    force_gate = _gate_needed(auth)

    while not stop.is_set():
        try:
            # ---- 登录门控：未登录则停留在此，直到登录成功 ---- #
            if force_gate or _gate_needed(auth):
                force_gate = False
                if not _wait_gate_login(api, auth, stop):
                    return      # 窗口已关闭，会话循环随之结束

            # ---- 进入应用 ---- #
            api.set_status("ready", "服务已就绪 ✓", f"正在载入本地应用 {url}")
            log.info("navigating to %s", url)
            time.sleep(0.5)  # let the splash repaint the "ready" state
            window.load_url(url)

            # endpoints.json 显式改动后的重绑定提示（改写动作在 main() 早期已完成）
            sync = getattr(api, "endpoint_sync", None) or {}
            if sync.get("changed"):
                api.toast(f"API 地址已按 endpoints.json 对齐：{sync.get('relay_base', '')}")

            # ---- 会话期：等待退出登录 ---- #
            while not logout_evt.wait(timeout=2.0):
                if stop.is_set():
                    return
            logout_evt.clear()

            # ---- 回到登录门控页：应用界面随页面卸载而不可用 ---- #
            log.info("logout detected; returning to login gate")
            api.set_status("login", "登录后开始使用", "")
            window.load_html(gate_page)
            force_gate = True  # 注销后必须重新登录才能再进应用
        except Exception:  # noqa: BLE001
            if stop.is_set():
                return          # 窗口已关闭，静默退出
            log.exception("session loop iteration failed")
            api.set_status("error", "会话异常，请关闭窗口后重新打开。",
                           "若反复失败，请把日志文件发给技术支持：\n" + str(LOG_FILE))
            return


# --------------------------------------------------------------------------- #
# bootstrap ------------------------------------------------------------------ #
def bootstrap(window, api: Api, auth: AuthManager) -> None:
    """runtime -> deeptutor start -> health check -> 登录门控会话循环。"""
    proc: DeepTutorProcess | None = None
    try:
        # 1. runtime (venv/portable-node/deeptutor; dev = system PATH)
        api.set_status("boot", "正在准备运行环境…", "检查 Python / Node / EduBuddy")
        deeptutor, node_dir = rt.ensure_runtime(on_line=api.push_line)
        if deeptutor is None:
            raise RuntimeError(
                "未找到 EduBuddy / Node.js 运行时。\n"
                "请先安装：pip install -U deeptutor 和 Node.js 20+\n"
                "（打包版安装器内置运行时，无需手动处理）"
            )
        if node_dir is None:
            raise RuntimeError(
                "未找到 Node.js（EduBuddy 需要 Node 20+ 才能启动前端）。请安装 Node.js。"
            )

        # 2. workspace
        home = rt.default_workspace()
        home.mkdir(parents=True, exist_ok=True)
        api.set_status("boot", "正在启动 EduBuddy 本地服务…", f"工作区：{home}")

        # 3. spawn hidden subprocess
        proc = DeepTutorProcess(
            deeptutor, home, node_dir, on_line=api.push_line
        )
        _shared["proc"] = proc
        proc.start()
        api.set_status(
            "boot",
            "正在启动本地服务…",
            f"{proc.frontend_url}（后端 :{proc.backend_port} + 前端 :{proc.frontend_port}）",
        )

        # 4. health check until the frontend answers
        url, _status = proc.wait_ready(timeout=150)
        _shared["deeptutor_version"] = rt.resolve_deeptutor_version() or "未知"

        # 5. 应用内「登录/账号」按钮 + 按登录态自绘的下拉菜单（后台线程；
        #    只在应用页面注入，门控页不受影响）。菜单动作统一装订。
        def _menu_refresh() -> None:
            res = api.refresh_models()
            if res.get("ok"):
                api.toast("模型已刷新 ✓")
                _reload_page()          # 让应用重新读取新写入的模型列表
            else:
                api.toast(res.get("message") or res.get("error") or "刷新失败")

        def _menu_logout() -> None:
            res = api.logout()
            if res.get("ok"):
                api.toast("已退出登录")
                # 通知会话主循环：导航回登录门控页
                evt = _shared.get("logout_requested")
                if isinstance(evt, threading.Event):
                    evt.set()
            else:
                api.toast("退出登录失败，请查看日志")

        def _menu_about() -> None:
            info = api.about()
            lines = [
                f"{info['app']}  v{info['app_version']}",
                f"EduBuddy 引擎：v{info['deeptutor_version']}",
            ]
            if info.get("relay"):
                lines.append(f"中继：{info['relay']}")
            dialogs.message_box(f"关于 {APP_NAME}", "\n".join(lines))

        injector = LoginButtonInjector(
            window,
            on_login=api.login,
            status_of=api.auth_status,
            on_authenticated=_reload_page,
            expect_url=url,
            menu_actions={
                "switch": api.login,
                "platform": api.open_platform,
                "refresh": _menu_refresh,
                "copy": api.copy_relay,
                "logout": _menu_logout,
                "about": _menu_about,
            },
            on_toast=api.toast,
        )
        _shared["injector"] = injector
        threading.Thread(target=injector.run, daemon=True).start()

        # 6. 登录门控会话主循环（门控 ↔ 应用，退出登录即回门控页）
        stop_evt = threading.Event()
        _shared["session_stop"] = stop_evt
        run_session(window, api, url, auth, stop_evt)
    except Exception as exc:  # noqa: BLE001
        log.exception("bootstrap failed")
        if proc:
            proc.stop()
            _shared["proc"] = None
        # keep it friendly for end users; full traceback lives in the log file
        if DEBUG:
            api.set_status("error", f"启动失败：{exc}", "")
        else:
            api.set_status(
                "error",
                "启动失败，请关闭窗口后重新打开。",
                "若反复失败，请把日志文件发给技术支持：\n" + str(LOG_FILE),
            )


# --------------------------------------------------------------------------- #
# entry ---------------------------------------------------------------------- #
def main() -> int:
    _setup_logging()

    if not _single_instance():
        log.warning("another instance is already running")
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                0,
                "EduBuddy 已在运行，请直接切换到已打开的窗口。",
                APP_NAME,
                0x40,  # MB_ICONINFORMATION
            )
        except Exception:  # noqa: BLE001
            pass
        return 1

    try:
        import webview  # lazy so a missing dep still yields a readable error
    except Exception as exc:  # noqa: BLE001
        log.error("pywebview missing: %s", exc)
        return 2

    frontend_url = f"http://127.0.0.1:{DEFAULT_FRONTEND_PORT}"
    auth = AuthManager()
    api = Api(frontend_url, auth, debug=DEBUG)

    # 端点对齐要赶在 DeepTutor 后端起来之前做：catalog 改写完成后，
    # 后端首次读取拿到的就是 endpoints.json 指向的地址。
    try:
        api.endpoint_sync = auth.apply_endpoint_overrides()
    except Exception:  # noqa: BLE001  对齐失败不拦启动，日志里可查
        log.exception("端点对齐失败（忽略，继续启动）")
        api.endpoint_sync = {}
    window = webview.create_window(
        "",
        html=splash_html(debug=DEBUG, version=__version__),
        width=1280,
        height=860,
        min_size=(1024, 680),
        js_api=api,
        background_color="#ffffff",
    )
    webview_windows.append(window)

    window.events.closed += _on_closed
    window.events.shown += lambda: _strip_titlebar_chrome(window)

    try:
        # webview.start(func) runs func after the event loop is ready →
        # the bootstrap thread only starts once the window object exists.
        webview.start(
            lambda: threading.Thread(
                target=bootstrap, args=(window, api, auth), daemon=True
            ).start(),
            debug=False,
        )
    finally:
        _shutdown()
    return 0


def _strip_titlebar_chrome(window) -> None:
    """去掉窗口标题栏的图标（标题文字已在 create_window 时设为空）。"""
    try:
        hwnd = getattr(window, "hwnd", None) or getattr(window, "_hwnd", None)
        if not hwnd:
            return
        user32 = ctypes.windll.user32
        WM_SETICON = 0x0080
        ICON_SMALL = 0
        ICON_BIG = 1
        user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, 0)
        user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, 0)
    except Exception:  # noqa: BLE001
        pass


def _on_closed() -> None:
    log.info("window closed; stopping deeptutor")
    stop_evt = _shared.get("session_stop")
    if isinstance(stop_evt, threading.Event):
        stop_evt.set()
    inj = _shared.get("injector")
    if inj:
        try:
            inj.stop()
        except Exception:  # noqa: BLE001
            pass
        _shared["injector"] = None
    proc = _shared["proc"]
    if proc:
        try:
            proc.stop()
        except Exception:  # noqa: BLE001
            pass
        _shared["proc"] = None


def _shutdown() -> None:
    _on_closed()
    lock = rt.ROOT / "app.lock"
    try:
        lock.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass
    log.info("exited cleanly")


if __name__ == "__main__":
    raise SystemExit(main())
