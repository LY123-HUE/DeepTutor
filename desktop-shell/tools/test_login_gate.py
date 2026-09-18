"""登录门控会话循环的无头验证（不启动 WebView / 不发网络请求）。

用真实的 AuthManager（临时 root/home）+ 桩 window/api 驱动
desktop.main.run_session，验证新的登录交互闭环：

  1. 未登录启动        → 停在登录门控（status=login，不进应用）
  2. 登录成功          → 进入应用（load_url）
  3. 应用内退出登录     → 导航回登录门控页（load_html，页面带 needlogin 门控态）
  4. 再次登录成功       → 重新进入应用
  5. 已登录启动        → 跳过门控直接进应用
  6. SKIP_LOGIN=1      → 跳过门控直接进应用
  7. splash_html(gate) → 门控态渲染（needlogin 类 + 登录按钮 + 无进度条）
 10. 退出登录清除陈旧登录结果（wait_done 不再秒过，2026-09-18 事故根因）
 11. 会话级复现：未消费登录 + 注销 → 停在门控页等待全新登录
 12. 注销后 catalog 残留密钥仍强制门控（force_gate）

运行：.venv/Scripts/python tools/test_login_gate.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from desktop.auth import AuthManager
from desktop.auth.store import TokenStore
from desktop import main as dt_main
from desktop.splash import splash_html

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f"  [{detail}]" if detail and not cond else ""))


class FakeApi:
    """记录 set_status 调用的 Api 桩。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def set_status(self, phase: str, text: str, detail: str = "") -> None:
        self.calls.append((phase, text))

    def last(self) -> tuple[str, str]:
        return self.calls[-1] if self.calls else ("", "")

    def phases(self, phase: str) -> list[str]:
        return [t for p, t in self.calls if p == phase]


class FakeWindow:
    """记录 load_url / load_html 的窗口桩。"""

    def __init__(self) -> None:
        self.loaded_urls: list[str] = []
        self.loaded_html: list[str] = []

    def load_url(self, url: str) -> None:
        self.loaded_urls.append(url)

    def load_html(self, html: str) -> None:
        self.loaded_html.append(html)


def make_manager(base: Path) -> AuthManager:
    """在隔离目录里构建真实 AuthManager（不触网）。"""
    root = base / f"root-{uuid.uuid4().hex[:8]}"
    home = base / f"home-{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    return AuthManager(root=root, home=home)


def store_token(mgr: AuthManager, token: str = "sk-Tok-test") -> None:
    """直接落一份已登录 payload（等价于登录成功的落盘结果）。"""
    store: TokenStore = mgr._store
    store.save({
        "token": token,
        "access_token": "access-x",
        "refresh_token": "",
        "account": {"phone": "13800001234", "models": ["m1"]},
        "relay_base": "https://relay.example/v1",
        "relay_source": "local-config",
    })


def finalize_login(mgr: AuthManager) -> None:
    """模拟一次成功登录的收尾（watchdog 的最后一步）。"""
    mgr._finalize({"ok": True, "token": "sk-Tok-new", "models": ["m1", "m2"]})


def stop_session(stop: threading.Event, logout_evt: threading.Event) -> None:
    logout_evt.set()          # 打断会话期 wait
    stop.set()                # 打破门控期 wait_done 循环（下一轮退出）
    # 门控期 wait_done(timeout=2) 最多再等 2s；join 时留足余量


def scenario_gate_then_login(base: Path) -> None:
    print("[1-4] 未登录 → 门控 → 登录进应用 → 退出回门控 → 再登录进应用")
    mgr = make_manager(base)
    api, win = FakeApi(), FakeWindow()
    stop = threading.Event()
    t = threading.Thread(
        target=dt_main.run_session, args=(win, api, "http://127.0.0.1:3782", mgr, stop),
        daemon=True,
    )
    t.start()
    time.sleep(1.2)

    check("未登录启动停在登录门控（status=login）",
          any(p == "login" for p, _ in api.calls), str(api.calls))
    check("门控期未进入应用", not win.loaded_urls and not win.loaded_html)

    finalize_login(mgr)                      # 模拟 OAuth 完成
    time.sleep(1.5)
    check("登录成功后进入应用（load_url）", len(win.loaded_urls) == 1)
    check("进入前显示就绪状态",
          any("服务已就绪" in txt for _, txt in api.calls))

    # 模拟应用内退出登录：先清凭据（真实流程 api.logout() 已清），再发信号
    mgr._store.clear()
    logout_evt = dt_main._shared.get("logout_requested")
    check("会话主循环注册了 logout 信号", isinstance(logout_evt, threading.Event))
    logout_evt.set()
    time.sleep(1.5)
    check("退出后导航回登录门控页（load_html）", len(win.loaded_html) == 1)
    if win.loaded_html:
        html = win.loaded_html[-1]
        check("门控页带 needlogin 门控态渲染", "needlogin" in html)
        check("门控页包含黑色登录按钮", 'id="btnLogin"' in html)
        check("门控页不渲染启动进度条", 'class="bar" id="bar"' in html)
    check("退出后回到门控等待状态",
          any(p == "login" for p, _ in api.calls[-4:]), str(api.calls[-4:]))

    finalize_login(mgr)                      # 再次登录
    time.sleep(1.5)
    check("再次登录后重新进入应用", len(win.loaded_urls) == 2)

    stop_session(stop, dt_main._shared.get("logout_requested") or threading.Event())
    t.join(timeout=6)
    check("会话循环随 stop 退出", not t.is_alive())


def scenario_already_logged_in(base: Path) -> None:
    print("[5] 已登录启动 → 跳过门控直接进应用")
    mgr = make_manager(base)
    store_token(mgr)
    api, win = FakeApi(), FakeWindow()
    stop = threading.Event()
    t = threading.Thread(
        target=dt_main.run_session, args=(win, api, "http://127.0.0.1:3782", mgr, stop),
        daemon=True,
    )
    t.start()
    time.sleep(1.2)
    check("已登录不进门控", not any(p == "login" for p, _ in api.calls))
    check("已登录直接进入应用", len(win.loaded_urls) == 1)
    stop_session(stop, dt_main._shared.get("logout_requested") or threading.Event())
    t.join(timeout=6)
    check("会话循环退出", not t.is_alive())


def scenario_skip_login_env(base: Path) -> None:
    print("[6] DEEPTUTOR_DESKTOP_SKIP_LOGIN=1 → 跳过门控")
    mgr = make_manager(base)
    api, win = FakeApi(), FakeWindow()
    stop = threading.Event()
    os.environ["DEEPTUTOR_DESKTOP_SKIP_LOGIN"] = "1"
    try:
        t = threading.Thread(
            target=dt_main.run_session,
            args=(win, api, "http://127.0.0.1:3782", mgr, stop),
            daemon=True,
        )
        t.start()
        time.sleep(1.2)
        check("SKIP_LOGIN 旁路生效（不进门控）",
              not any(p == "login" for p, _ in api.calls))
        check("SKIP_LOGIN 直接进入应用", len(win.loaded_urls) == 1)
    finally:
        os.environ.pop("DEEPTUTOR_DESKTOP_SKIP_LOGIN", None)
        stop_session(stop, dt_main._shared.get("logout_requested") or threading.Event())
        t.join(timeout=6)


def scenario_gate_page_render() -> None:
    print("[7] splash_html 门控态渲染")
    html = splash_html(debug=False, version="0.1.0", gate=True)
    check("gate=True 预置 needlogin 门控态", "needlogin" in html)
    check("包含登录按钮与平台注册入口",
          'id="btnLogin"' in html and 'id="btnPlatform"' in html)
    check("初始状态行为门控文案", "登录后开始使用" in html)
    check("注入版本号", "v0.1.0" in html)
    boot = splash_html(debug=False, version="0.1.0", gate=False)
    check("boot 态 html 标签无门控预置", '<html lang="zh-CN" class="">' in boot)
    check("gate 态 html 标签带 needlogin", '<html lang="zh-CN" class="needlogin">' in html)


def scenario_bootstrap_wiring(base: Path) -> None:
    """bootstrap 接线层冒烟：mock 运行时与子进程，验证真实 bootstrap → 门控 → 进应用。

    背景：run_session 桩测试抓不住 bootstrap 层的名字/接线错误
    （2026-09-18 NameError: auth is not defined 事故），本场景补上这一层。
    """
    print("[8] bootstrap 接线冒烟（mock ensure_runtime / DeepTutorProcess）")
    orig_ensure = dt_main.rt.ensure_runtime
    orig_ws = dt_main.rt.default_workspace
    orig_ver = dt_main.rt.resolve_deeptutor_version
    orig_proc = dt_main.DeepTutorProcess
    ws = base / "ws-smoke"

    class FakeProc:
        frontend_url = "http://127.0.0.1:3782"
        frontend_port = 3782
        backend_port = 8001

        def __init__(self, *a, **k) -> None:
            pass

        def start(self) -> None:
            pass

        def wait_ready(self, timeout: float = 150):
            return (self.frontend_url, 200)

        def stop(self) -> None:
            pass

    dt_main.rt.ensure_runtime = lambda on_line=None: ("deeptutor-fake", "node-fake")
    dt_main.rt.default_workspace = lambda: ws
    dt_main.rt.resolve_deeptutor_version = lambda: "1.6.8"
    dt_main.DeepTutorProcess = FakeProc
    try:
        mgr = make_manager(base)
        api = dt_main.Api("http://127.0.0.1:3782", mgr, debug=False)
        win = FakeWindow()
        t = threading.Thread(
            target=dt_main.bootstrap, args=(win, api, mgr), daemon=True
        )
        t.start()
        time.sleep(1.5)
        st = api.status()
        check("bootstrap 未登录停在门控（phase=login）", st["phase"] == "login",
              str(st))
        check("bootstrap 门控期未进入应用", not win.loaded_urls and not win.loaded_html)

        finalize_login(mgr)          # 模拟 OAuth 完成
        time.sleep(1.5)
        check("bootstrap 登录成功后进入应用", len(win.loaded_urls) == 1)
        check("bootstrap 未触发错误状态", api.status()["phase"] != "error",
              str(api.status()))

        # 收尾：触发退出 + 停止会话循环
        logout_evt = dt_main._shared.get("logout_requested")
        stop_evt = dt_main._shared.get("session_stop")
        if isinstance(logout_evt, threading.Event):
            logout_evt.set()
        if isinstance(stop_evt, threading.Event):
            stop_evt.set()
        inj = dt_main._shared.get("injector")
        if inj is not None:
            inj.stop()
        t.join(timeout=8)
        check("bootstrap 会话循环随 stop 退出", not t.is_alive())
    finally:
        dt_main.rt.ensure_runtime = orig_ensure
        dt_main.rt.default_workspace = orig_ws
        dt_main.rt.resolve_deeptutor_version = orig_ver
        dt_main.DeepTutorProcess = orig_proc


def scenario_splash_js_smoke() -> None:
    """[9] splash 页面 JS 真实执行冒烟（node + DOM 桩，见 tools/splash_js_smoke.js）。

    背景：模板 JS 经 Python 转义后可能语法死亡（2026-09-18 事故，脚本一行不
    执行、门控页永不出现，静态预览无法发现）。此场景提取 splash_html 发射的
    <script>，交给 node 真实执行并断言状态机行为；node 缺失时跳过并告警。
    """
    import re
    import shutil
    import subprocess

    print("[9] splash JS 运行时冒烟（node + DOM 桩）")
    node = shutil.which("node")
    if not node:
        print("  ⚠ 未找到 node，跳过（桌面壳开发环境需要 Node 20+，请安装后重跑）")
        return
    html = splash_html(debug=False, version="0.1.1", gate=False)
    m = re.search(r"<script>(.*?)</script>", html, re.S)
    check("splash 模板包含 <script> 块", m is not None)
    if m is None:
        return
    script_path = Path(tempfile.gettempdir()) / f"edubuddy_splash_{uuid.uuid4().hex[:8]}.js"
    script_path.write_text(m.group(1), encoding="utf-8")
    try:
        proc = subprocess.run(
            [node, str(Path(__file__).parent / "splash_js_smoke.js"), str(script_path)],
            capture_output=True, text=True, timeout=60,
        )
        tail = (proc.stdout or "").strip().splitlines()[-6:]
        for line in tail:
            print("   ", line)
        check("splash JS 真实执行全链路断言通过",
              proc.returncode == 0 and "SPLASH_JS_SMOKE_OK" in proc.stdout,
              (proc.stderr or proc.stdout or "")[-300:])
    except subprocess.TimeoutExpired:
        check("splash JS 冒烟未超时", False, "node runner 超时（疑似脚本内死循环）")
    finally:
        script_path.unlink(missing_ok=True)


def scenario_stale_login_result(base: Path) -> None:
    """[10] 退出登录必须清掉陈旧的登录等待状态（2026-09-18 事故根因）。

    事故：应用内「切换账号」再登录成功时 _done 置位但无人消费；随后退出
    登录，门控等待 wait_done() 立刻拿到陈旧的 {"ok": True} 直接放行，
    7ms 内闪回应用。修复后 logout() 清 _done/_result/_pending_*。
    """
    print("[10] logout 清除陈旧登录结果（wait_done 不再秒过）")
    mgr = make_manager(base)
    mgr._client.revoke = lambda *a, **k: None      # 断网安全：吊销桩掉
    store_token(mgr)                               # 已登录态
    finalize_login(mgr)                            # 一次未被消费的成功登录
    check("前置：_done 处于置位（陈旧成功待消费）", mgr._done.is_set())

    mgr.logout()
    check("logout 后 _done 已清空", not mgr._done.is_set())
    result = mgr.wait_done(timeout=0.5)
    check("门控等待不再拿到陈旧成功", result.get("ok") is not True, str(result))
    check("logout 后本地凭据已清空", not mgr.is_logged_in())
    os.environ.pop("DEEPTUTOR_DESKTOP_SKIP_LOGIN", None)
    check("logout 后判定需要门控", dt_main._gate_needed(mgr))


def scenario_logout_stays_on_gate(base: Path) -> None:
    """[11] 会话级复现事故时间线：未消费登录 + 注销 → 必须停在门控页。

    时间线复刻 2026-09-18 日志：
      门控登录进应用 → 应用内切换账号（登录成功但无人消费，_done 陈旧置位）
      → 退出登录 → 门控必须真正等待，绝不闪回应用。
    """
    print("[11] 注销后停在门控页等待全新登录（陈旧成功回归）")
    mgr = make_manager(base)
    mgr._client.revoke = lambda *a, **k: None
    api, win = FakeApi(), FakeWindow()
    stop = threading.Event()
    t = threading.Thread(
        target=dt_main.run_session, args=(win, api, "http://127.0.0.1:3782", mgr, stop),
        daemon=True,
    )
    t.start()
    time.sleep(1.2)
    check("启动停在门控", any(p == "login" for p, _ in api.calls))

    finalize_login(mgr)                    # 第 1 次登录：门控消费
    time.sleep(1.5)
    check("第 1 次登录进入应用", len(win.loaded_urls) == 1)

    finalize_login(mgr)                    # 第 2 次登录：模拟应用内切换账号，无人消费
    time.sleep(0.5)
    check("前置：第 2 次登录结果未被消费（_done 置位）", mgr._done.is_set())

    mgr.logout()                           # 退出登录（真实路径：清凭据 + 清等待态）
    logout_evt = dt_main._shared.get("logout_requested")
    check("会话主循环注册了 logout 信号", isinstance(logout_evt, threading.Event))
    if isinstance(logout_evt, threading.Event):
        logout_evt.set()
    time.sleep(2.5)
    check("注销后未闪回应用（无新 load_url）", len(win.loaded_urls) == 1,
          f"loaded_urls={len(win.loaded_urls)}")
    check("注销后停在登录门控页（load_html）", len(win.loaded_html) == 1)

    finalize_login(mgr)                    # 第 3 次登录：全新登录才放行
    time.sleep(2.0)
    check("全新登录后重新进入应用", len(win.loaded_urls) == 2)

    stop_session(stop, dt_main._shared.get("logout_requested") or threading.Event())
    t.join(timeout=6)
    check("会话循环随 stop 退出", not t.is_alive())


def scenario_logout_with_residual_catalog_key(base: Path) -> None:
    """[12] 注销后即使 catalog 残留可用密钥，也必须停在门控页（force_gate）。

    场景：用户在 DeepTutor 设置里手配过带 api_key 的 profile（或任何残留），
    has_configured_token()==True。旧逻辑 _gate_needed()==False 会跳过门控，
    刚 load_html 的登录页被 load_url 覆盖；修复后 force_gate 无条件门控。
    """
    print("[12] 注销后 catalog 残留密钥仍强制门控")
    from desktop.auth.catalog import ensure_tokengine_catalog

    mgr = make_manager(base)
    store_token(mgr)                       # 已登录启动 → 直接进应用
    api, win = FakeApi(), FakeWindow()
    stop = threading.Event()
    t = threading.Thread(
        target=dt_main.run_session, args=(win, api, "http://127.0.0.1:3782", mgr, stop),
        daemon=True,
    )
    t.start()
    time.sleep(1.2)
    check("已登录直接进入应用", len(win.loaded_urls) == 1)

    # 模拟注销后的残留：凭据已清，但 catalog 里还有一条带 api_key 的活动 profile
    mgr._store.clear()
    ensure_tokengine_catalog(
        home=mgr.home, api_key="sk-Tok-residual", base_url="https://relay.example/v1",
        models=["m1"],
    )
    check("前置：catalog 残留可用密钥", mgr.has_usable_token())

    logout_evt = dt_main._shared.get("logout_requested")
    if isinstance(logout_evt, threading.Event):
        logout_evt.set()
    time.sleep(2.5)
    check("残留密钥未导致闪回应用（无新 load_url）", len(win.loaded_urls) == 1,
          f"loaded_urls={len(win.loaded_urls)}")
    check("注销后停在登录门控页（load_html）", len(win.loaded_html) == 1)

    stop_session(stop, dt_main._shared.get("logout_requested") or threading.Event())
    t.join(timeout=6)
    check("会话循环随 stop 退出", not t.is_alive())


def main() -> int:
    print("=== EduBuddy 登录门控会话循环 无头验证 ===")
    with tempfile.TemporaryDirectory(prefix="edubuddy-gate-test-") as td:
        base = Path(td)
        scenario_gate_then_login(base)
        scenario_already_logged_in(base)
        scenario_skip_login_env(base)
        scenario_gate_page_render()
        scenario_bootstrap_wiring(base)
        scenario_splash_js_smoke()
        scenario_stale_login_result(base)
        scenario_logout_stays_on_gate(base)
        scenario_logout_with_residual_catalog_key(base)
    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：", "、".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
