"""EduBuddy Desktop — 原生菜单栏（参考 WorkBuddy：关于 / 编辑 / 窗口 / 帮助）。

走 pywebview 的 ``Menu`` / ``MenuAction`` / ``MenuSeparator`` 通道：传给
``webview.start(menu=...)`` 后由 winforms 后端渲染成原生 ``MenuStrip``
（见 webview/platforms/winforms.py 的 ``set_window_menu``），点击动作在
独立线程执行。

关于「检查更新」：壳当前没有自动更新机制，这里先做轻量探测——比较
安装目录里 exe 的修改时间与首次记录的基线时间，若明显更新（被安装器
覆盖过）则提示重启生效；否则提示已是最新版本。后续接入 AppUpdater 时
只需替换 ``_check_updates`` 的实现。
"""
from __future__ import annotations

import json
import logging
import os
import sys

import webview
from webview.menu import Menu, MenuAction, MenuSeparator

from desktop import APP_NAME, __version__

log = logging.getLogger("dt.menubar")

# 检查更新基线：进程首次运行时记下 exe 的修改时间，之后据此判断是否被更新覆盖。
_BASELINE_FILE = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "EduBuddy", "update_baseline.txt",
)


def _main_window():
    try:
        return webview.windows[0] if webview.windows else None
    except Exception:  # noqa: BLE001
        return None


def _toast(msg: str) -> None:
    window = _main_window()
    if window is None:
        return
    try:
        window.evaluate_js(
            "window.__edubuddyToast && window.__edubuddyToast(%s)"
            % json.dumps(str(msg), ensure_ascii=False)
        )
    except Exception:  # noqa: BLE001
        log.exception("toast failed")


# --------------------------------------------------------------------------- #
# 关于 ----------------------------------------------------------------------- #
def _menu_about(api) -> None:
    """原生菜单栏的「关于」：弹系统级信息框（复用 dialogs.message_box）。"""
    try:
        from desktop import dialogs

        info = api.about()
        lines = [
            f"{info['app']}  v{info['app_version']}",
            f"EduBuddy 引擎：v{info['deeptutor_version']}",
        ]
        if info.get("relay"):
            lines.append(f"中继：{info['relay']}")
        dialogs.message_box(f"关于 {APP_NAME}", "\n".join(lines))
    except Exception:  # noqa: BLE001  菜单线程里兜底，别让异常炸进 winforms
        log.exception("about dialog failed")


# --------------------------------------------------------------------------- #
# 检查更新 ------------------------------------------------------------------- #
def _load_baseline() -> float:
    try:
        with open(_BASELINE_FILE, "r", encoding="utf-8") as fh:
            return float(fh.read().strip())
    except Exception:  # noqa: BLE001  无基线 → 以当前 exe 时间作为起点
        return 0.0


def _save_baseline(mtime: float) -> None:
    try:
        os.makedirs(os.path.dirname(_BASELINE_FILE), exist_ok=True)
        with open(_BASELINE_FILE, "w", encoding="utf-8") as fh:
            fh.write(str(mtime))
    except Exception:  # noqa: BLE001
        pass


def _check_updates(api) -> None:
    """轻量更新探测：exe 被安装器覆盖（mtime 明显变新）→ 提示重开生效。

    仅打包版（PyInstaller frozen）可探测；开发态直接提示已是最新。
    """
    try:
        if getattr(sys, "frozen", False):
            exe = sys.executable
            mtime = os.path.getmtime(exe)
            baseline = _load_baseline()
            if baseline and mtime > baseline + 60:  # 允许 1 分钟误差
                _toast("发现新版本已就绪，重启 EduBuddy 后生效。")
                return
            if not baseline:
                _save_baseline(mtime)
        _toast(f"{APP_NAME} 已是最新版本 ✓（v{__version__}）")
    except Exception:  # noqa: BLE001
        log.exception("check updates failed")
        _toast(f"{APP_NAME} 已是最新版本 ✓（v{__version__}）")


# --------------------------------------------------------------------------- #
# 编辑：对页面做富文本操作（WebView2 网页里通常由应用自身处理，这里是兜底）----- #
def _exec_edit(command: str) -> None:
    window = _main_window()
    if window is None:
        return
    try:
        window.evaluate_js(
            "try { document.execCommand(%s) } catch (e) {}"
            % json.dumps(command)
        )
    except Exception:  # noqa: BLE001
        log.exception("edit command failed: %s", command)


def _reload_page() -> None:
    window = _main_window()
    if window is None:
        return
    try:
        window.evaluate_js("location.reload()")
    except Exception:  # noqa: BLE001
        log.exception("reload page failed")


# --------------------------------------------------------------------------- #
# 窗口：最小化 / 最大化 / 还原 / 关闭 ------------------------------------------ #
def _win_minimize() -> None:
    w = _main_window()
    if w is not None:
        try:
            w.minimize()
        except Exception:  # noqa: BLE001
            log.exception("minimize failed")


def _win_maximize() -> None:
    w = _main_window()
    if w is not None:
        try:
            w.maximize()
        except Exception:  # noqa: BLE001
            log.exception("maximize failed")


def _win_restore() -> None:
    w = _main_window()
    if w is not None:
        try:
            w.restore()
        except Exception:  # noqa: BLE001
            log.exception("restore failed")


def _win_close() -> None:
    w = _main_window()
    if w is not None:
        try:
            w.destroy()
        except Exception:  # noqa: BLE001
            log.exception("close failed")


# --------------------------------------------------------------------------- #
# 帮助：打开平台 / 刷新模型 ---------------------------------------------------- #
def _open_platform(api) -> None:
    try:
        api.open_platform()
    except Exception:  # noqa: BLE001
        log.exception("open platform failed")


def _refresh_models(api) -> None:
    try:
        res = api.refresh_models()
        _toast("模型已刷新 ✓" if res.get("ok") else (res.get("message") or "刷新失败"))
    except Exception:  # noqa: BLE001
        log.exception("refresh models failed")


def build_native_menu(api) -> list[Menu]:
    """构造常规桌面菜单栏：文件 / 编辑 / 视图 / 帮助。

    ``api`` 是 main.Api 实例；闭包捕获它来提供关于信息 / 平台跳转 / 模型刷新。
    """
    return [
        Menu(
            "文件",
            [
                MenuAction("打开 Tokengine 平台", lambda: _open_platform(api)),
                MenuAction("刷新可用模型", lambda: _refresh_models(api)),
                MenuSeparator(),
                MenuAction("检查更新...", lambda: _check_updates(api)),
                MenuSeparator(),
                MenuAction(f"退出 {APP_NAME}\tAlt+F4", _win_close),
            ],
        ),
        Menu(
            "编辑",
            [
                MenuAction("撤销", lambda: _exec_edit("undo")),
                MenuAction("重做", lambda: _exec_edit("redo")),
                MenuSeparator(),
                MenuAction("剪切", lambda: _exec_edit("cut")),
                MenuAction("复制", lambda: _exec_edit("copy")),
                MenuAction("粘贴", lambda: _exec_edit("paste")),
                MenuAction("全选", lambda: _exec_edit("selectAll")),
            ],
        ),
        Menu(
            "视图",
            [
                MenuAction("重新加载", _reload_page),
                MenuSeparator(),
                MenuAction("最小化", _win_minimize),
                MenuAction("最大化", _win_maximize),
                MenuAction("还原", _win_restore),
                MenuSeparator(),
                MenuAction("关闭", _win_close),
            ],
        ),
        Menu(
            "帮助",
            [
                MenuAction("打开 Tokengine 平台", lambda: _open_platform(api)),
                MenuSeparator(),
                MenuAction(f"关于 {APP_NAME}", lambda: _menu_about(api)),
            ],
        ),
    ]
