"""PKCE（RFC 7636）：code_verifier 生成 + S256 code_challenge。"""
from __future__ import annotations

import base64
import hashlib
import secrets

_VERIFIER_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def generate_verifier(length: int = 64) -> str:
    """生成 43-128 位无填充 URL-safe 随机串（默认 64 位）。"""
    if not 43 <= length <= 128:
        raise ValueError("code_verifier length must be in 43..128")
    return "".join(secrets.choice(_VERIFIER_ALPHABET) for _ in range(length))


def s256_challenge(verifier: str) -> str:
    """SHA-256(verifier) 后 base64url 无填充，构成 S256 code_challenge。"""
    return _b64url(hashlib.sha256(verifier.encode("utf-8")).digest())


def generate_state() -> str:
    """防 CSRF 的 state 参数。"""
    return _b64url(secrets.token_bytes(24))
