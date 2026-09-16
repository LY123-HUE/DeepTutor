"""令牌安全落盘。

- 业务令牌（sk-Tok...）与 refresh_token 用 Windows DPAPI（CryptProtectData）
  加密后写入 %LOCALAPPDATA%\\EduBuddy\\auth.json —— 仅当前 Windows 用户可解密。
- 机器指纹 machine_id 单独一个文件，用于平台侧设备绑定。
- 全程不透出明文到日志；加密失败时给出一条可见的降级告警（仅提示，不打值）。
"""
from __future__ import annotations

import base64
import ctypes
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("dt.auth.store")

_DPAPI_UI_FORBIDDEN = 0x00000001


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.c_void_p)]


def _dpapi_protect(plain: bytes) -> bytes:
    """DPAPI 加密（仅可被当前用户解密）。"""
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(plain, len(plain))
    blob_in = _DataBlob(len(plain), ctypes.cast(buf, ctypes.c_void_p))
    blob_out = _DataBlob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None,
        _DPAPI_UI_FORBIDDEN, ctypes.byref(blob_out),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        raw = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(ctypes.c_void_p(blob_out.pbData))
    return bytes(raw)


def _dpapi_unprotect(cipher: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(cipher, len(cipher))
    blob_in = _DataBlob(len(cipher), ctypes.cast(buf, ctypes.c_void_p))
    blob_out = _DataBlob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None,
        _DPAPI_UI_FORBIDDEN, ctypes.byref(blob_out),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        raw = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(ctypes.c_void_p(blob_out.pbData))
    return bytes(raw)


class TokenStore:
    """读取/保存登录凭证。payload 结构：

    {
      "version": 1,
      "token": "sk-Tok...",            # 业务令牌（唯一写入 model catalog 的）
      "access_token": "...",           # OAuth 会话凭证（仅换/吊销用）
      "refresh_token": "...",
      "expires_at": 1768400000,        # access_token 过期 epoch
      "account": {"phone": "...", "models": [...], "balance": ...},
      "machine_id": "...",
      "encrypted": true
    }
    """

    FILE_NAME = "auth.json"
    MACHINE_FILE_NAME = "machine_id"

    def __init__(self, root: Path) -> None:
        self._path = root / self.FILE_NAME
        self._machine_path = root / self.MACHINE_FILE_NAME
        root.mkdir(parents=True, exist_ok=True)

    # -- machine_id ------------------------------------------------------ #
    def machine_id(self) -> str:
        """读取或生成稳定的本机标识（与桌面端复用同一份）。"""
        if self._machine_path.exists():
            value = self._machine_path.read_text(encoding="utf-8").strip()
            if value:
                return value
        value = uuid.uuid4().hex
        self._machine_path.write_text(value, encoding="utf-8")
        return value

    # -- auth payload ---------------------------------------------------- #
    def save(self, payload: dict[str, Any]) -> None:
        payload = dict(payload)
        payload["version"] = 1
        payload["machine_id"] = self.machine_id()
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        try:
            blob = _dpapi_protect(text.encode("utf-8"))
            body = {"encrypted": True, "blob": base64.b64encode(blob).decode("ascii")}
        except Exception as exc:  # noqa: BLE001
            log.warning("DPAPI 加密失败，凭证将以明文降级存储（%s）", exc)
            body = {"encrypted": False, "blob": text}
        self._atomic_write(json.dumps(body, ensure_ascii=False, indent=2))

    def load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            body = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        try:
            if body.get("encrypted"):
                cipher = base64.b64decode(body.get("blob", ""))
                text = _dpapi_unprotect(cipher).decode("utf-8")
            else:
                text = body.get("blob", "")
            payload = json.loads(text)
            return payload if isinstance(payload, dict) else {}
        except Exception as exc:  # noqa: BLE001
            log.warning("令牌读取失败（可能已改名/无权限），视为未登录：%s", exc)
            return {}

    def clear(self) -> None:
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            pass

    # -- helpers --------------------------------------------------------- #
    def _atomic_write(self, text: str) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self._path)
