"""EduBuddy 桌面端 × Tokengine 平台 —— Token 中转登录模块。

把 Tokengine 平台的令牌（业务令牌，形如 sk-Tok...）经 OAuth 授权码 + PKCE
流程取回，安全落盘，并写入 DeepTutor 的 model catalog，让用户登录后即可对话。

部署形态：该包运行在 EduBuddyDesktop.exe 的 PyInstaller 环境内（非 DeepTutor
运行时环境），因此只用 Python 标准库（http.server / urllib / ctypes/DPAPI），
不依赖 requests / win32crypt 等第三方库。
"""
from __future__ import annotations

from .manager import AuthManager
from .catalog import ensure_tokengine_catalog, has_configured_token

__all__ = ["AuthManager", "ensure_tokengine_catalog", "has_configured_token"]
__version__ = "0.1.0"
