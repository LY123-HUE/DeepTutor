"""原生 Windows 文本框（纯 ctypes，PyInstaller 安全）。

右键菜单「关于 EduBuddy」用原生 MessageBox，避免依赖页面 DOM 拉一个
需要 re-render 的弹层。图标常量沿用 Win32 定义：
  MB_ICONINFORMATION = 0x40      MB_OK = 0x0
  MB_ICONWARNING     = 0x30      MB_OKCANCEL = 0x1
"""
from __future__ import annotations

import ctypes
import logging

log = logging.getLogger("dt.dialogs")


def message_box(title: str, text: str, icon: int = 0x40) -> int:
    """弹一个模态信息框；owner=None（无父窗口）。返回用户点击的按钮 ID。"""
    try:
        return ctypes.windll.user32.MessageBoxW(0, text, title, icon)
    except Exception as exc:  # noqa: BLE001
        log.exception("message box failed: %s", exc)
        return 0
