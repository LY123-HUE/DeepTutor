"""Tokengine OAuth 端点客户端（纯 urllib，无第三方依赖）。"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from . import config as cfg

log = logging.getLogger("dt.auth.client")


class OAuthError(RuntimeError):
    def __init__(self, message: str, code: int = 0, payload: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.payload = payload or {}


# 顶层只要出现这些键中任意一个，就认为响应已经是"扁平的"，不再下钻。
_FLAT_HINTS = (
    "token", "access_token", "refresh_token", "expires_in",
    "models", "ai_token", "phone", "balance",
    "relay_base", "base_url", "domain",
)


def unwrap(payload: Any) -> dict[str, Any]:
    """剥掉网关常见的 ``{"success":true,"data":{...}}`` 外壳。

    这个平台（new-api 系）惯于把业务数据包在 ``data`` 里。若不解包，
    ``models`` / ``phone`` / 域名等字段会全部取不到，表现为"登录成功但什么都没拉到"
    并且**没有任何报错**——静默失效最难查，所以在这一层统一兼容。

    规则：顶层已有我们认识的键 → 原样返回；否则 ``data`` 是 dict → 下钻一层。
    """
    if not isinstance(payload, dict):
        return {}
    if any(key in payload for key in _FLAT_HINTS):
        return payload
    inner = payload.get("data")
    if isinstance(inner, dict):
        return inner
    return payload


def _post_form(url: str, data: dict[str, Any], timeout: float = 20.0) -> dict[str, Any]:
    body = urllib.parse.urlencode(data).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            text = resp.read().decode("utf-8")
            return unwrap(json.loads(text)) if text else {}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        payload = None
        try:
            payload = json.loads(detail)
        except (ValueError, TypeError):
            pass
        log.error("OAuth %s -> HTTP %s: %s", url, exc.code, detail[:300])
        raise OAuthError(f"平台返回错误（HTTP {exc.code}）", exc.code, payload) from exc
    except urllib.error.URLError as exc:
        log.error("OAuth network error: %s", exc)
        raise OAuthError(f"无法连接平台（{exc.reason}）") from exc


def _get_json(url: str, token: str, timeout: float = 20.0) -> dict[str, Any]:
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            text = resp.read().decode("utf-8")
            return unwrap(json.loads(text)) if text else {}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        raise OAuthError(f"userinfo 失败（HTTP {exc.code}）：{detail[:200]}", exc.code) from exc
    except urllib.error.URLError as exc:
        raise OAuthError(f"userinfo 网络错误：{exc.reason}") from exc


class OAuthClient:
    def __init__(
        self,
        authorize_url: str = cfg.AUTHORIZE_URL,
        token_url: str = cfg.TOKEN_URL,
        userinfo_url: str = cfg.USERINFO_URL,
        revoke_url: str = cfg.REVOKE_URL,
        client_id: str = cfg.CLIENT_ID,
        scope: str = cfg.SCOPE,
    ) -> None:
        self.authorize_url = authorize_url
        self.token_url = token_url
        self.userinfo_url = userinfo_url
        self.revoke_url = revoke_url
        self.client_id = client_id
        self.scope = scope

    # -- URL 构造 -------------------------------------------------------- #
    def build_authorize_url(
        self,
        redirect_uri: str,
        code_challenge: str,
        state: str,
        machine_id: str,
        prompt: str = "",
    ) -> str:
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": self.scope,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "auth_type": "local",
            "login_channel": "native_desktop",
            "machine_id": machine_id,
            "x_machine_id": machine_id,
        }
        # prompt="login"（OIDC 语义：强制重新认证）：切换账号时携带，
        # 平台据此跳过「已有会话直接发码」，强制进授权页让用户选账号。
        # 旧版平台不识别该参数会忽略之，行为退化为原样（自动授权当前会话）。
        if prompt:
            params["prompt"] = prompt
        return f"{self.authorize_url}?{urllib.parse.urlencode(params)}"

    # -- token 交换 ------------------------------------------------------ #
    def exchange(self, code: str, code_verifier: str, redirect_uri: str,
                 machine_id: str) -> dict[str, Any]:
        return _post_form(self.token_url, {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self.client_id,
            "code_verifier": code_verifier,
            "machine_id": machine_id,
        })

    def refresh(self, refresh_token: str, machine_id: str) -> dict[str, Any]:
        return _post_form(self.token_url, {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.client_id,
            "scope": self.scope,
            "machine_id": machine_id,
        })

    def revoke(self, token: str, machine_id: str) -> None:
        if not self.revoke_url:
            return
        try:
            _post_form(self.revoke_url, {"token": token, "machine_id": machine_id}, timeout=10)
        except OAuthError as exc:
            log.warning("revoke 未清理平台侧（可忽略）：%s", exc)

    # -- 用户信息 -------------------------------------------------------- #
    def userinfo(self, access_token: str) -> dict[str, Any]:
        return _get_json(self.userinfo_url, access_token)
