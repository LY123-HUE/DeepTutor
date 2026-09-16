"""Windows 剪贴板写入（纯 ctypes，无第三方依赖，PyInstaller 安全）。

只在 Windows 桌面壳里用（右键菜单「复制 API 地址」）。刻意不依赖
pywebview 的 clipboard（版本兼容性）与第三方库（壳环境最小化）。
"""
from __future__ import annotations

import ctypes
import logging

log = logging.getLogger("dt.clipboard")

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def set_text(text: str) -> bool:
    """把文本放入系统剪贴板（Unicode）。失败返回 False 并记日志。"""
    if not text:
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        if not user32.OpenClipboard(None):
            log.warning("OpenClipboard failed")
            return False
        try:
            user32.EmptyClipboard()
            data = (text + "\0").encode("utf-16-le")
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not handle:
                return False
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                kernel32.GlobalFree(handle)
                return False
            try:
                ctypes.memmove(ptr, data, len(data))
            finally:
                kernel32.GlobalUnlock(handle)
            # SetClipboardData 接管 handle 所有权，系统负责释放，勿再 GlobalFree
            if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                kernel32.GlobalFree(handle)
                return False
            return True
        finally:
            user32.CloseClipboard()
    except Exception:  # noqa: BLE001
        log.exception("set clipboard failed")
        return False
