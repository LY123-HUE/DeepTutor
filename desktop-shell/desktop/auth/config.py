"""Tokengine 接入配置。

端点解析优先级（高 → 低）：

1. 环境变量（联调最快，无需改文件）
2. ``<ROOT>/endpoints.json``（打包版可外部覆盖，免重建就能指向本地平台）
3. 内置默认（当前构建为联调地址 ``http://127.0.0.1:3000``；发布用 ``PROD_API_BASE``）

``endpoints.json`` 示例（放在 ``%LOCALAPPDATA%\\EduBuddy\\endpoints.json``）::

    {
      "api_base": "http://127.0.0.1:3000",
      "relay_base": "http://127.0.0.1:3000/v1"
    }

可覆写的键：``api_base`` / ``authorize_url`` / ``token_url`` / ``userinfo_url`` /
``revoke_url`` / ``relay_base``。

环境变量名：``TOKENGINE_`` + 上述键的大写形式。
"""
from __future__ import annotations

import json
import logging
import os
import urllib.parse
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("dt.auth.config")

# 内置默认平台地址。
#
# 当前构建用于**本地联调**，默认指向本机跑的 Tokengine（D:\studio\tokengine，PORT=3000）。
# 发布前改回生产地址即可，两种方式任选：
#   1) 修改下面这一行的第二个参数为 "https://tokengine.hanyoai.com"
#   2) 构建时设 TOKENGINE_DEFAULT_API_BASE=https://tokengine.hanyoai.com
#
# 运行时仍可覆盖（优先级更高）：环境变量 TOKENGINE_API_BASE > <ROOT>/endpoints.json。
DEV_API_BASE = "http://127.0.0.1:3000"
PROD_API_BASE = "https://tokengine.hanyoai.com"

DEFAULT_API_BASE = os.environ.get("TOKENGINE_DEFAULT_API_BASE", DEV_API_BASE)

# 平台公开状态接口（无需鉴权）。new-api 系在 data.server_address 里给出
# **平台对外域名**——这是「登录拉取域名」的权威来源：运维换域名，客户端自动跟随。
STATUS_PATH = "/api/status"

# --------------------------------------------------------------------------- #
# 端点解析
# --------------------------------------------------------------------------- #
_ENDPOINT_KEYS = (
    "api_base",
    "authorize_url",
    "token_url",
    "userinfo_url",
    "revoke_url",
    "relay_base",
    "status_url",
)


def _read_endpoints_file(root: Optional[Path]) -> dict[str, str]:
    """读取 <root>/endpoints.json（打包版的外部覆盖点）。缺失/损坏都静默回退。"""
    if root is None:
        return {}
    path = Path(root) / "endpoints.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("endpoints.json 解析失败，忽略：%s", exc)
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for key in _ENDPOINT_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()
    if out:
        log.info("endpoints.json 覆盖生效：%s", sorted(out))
    return out


def endpoints(root: Optional[Path] = None) -> dict[str, str]:
    """解析最终生效的端点。所有值尾部无 ``/``。"""
    file_over = _read_endpoints_file(root)

    def pick(env_suffix: str, default: str) -> str:
        env_value = os.environ.get(f"TOKENGINE_{env_suffix.upper()}")
        value = env_value or file_over.get(env_suffix) or default
        return str(value).rstrip("/")

    base = pick("api_base", DEFAULT_API_BASE)
    resolved = {
        "api_base": base,
        "authorize_url": pick("authorize_url", f"{base}/oauth/authorize"),
        "token_url": pick("token_url", f"{base}/oauth/token"),
        "userinfo_url": pick("userinfo_url", f"{base}/oauth/userinfo"),
        "revoke_url": pick("revoke_url", f"{base}/oauth/revoke"),
        "relay_base": pick("relay_base", f"{base}/v1"),
        "status_url": pick("status_url", f"{base}{STATUS_PATH}"),
    }
    log.info("端点解析结果：api_base=%s authorize=%s relay=%s",
             resolved["api_base"], resolved["authorize_url"], resolved["relay_base"])
    return resolved


def explicit_overrides(root: Optional[Path] = None) -> dict[str, str]:
    """收集**显式**配置的端点键值（环境变量 > endpoints.json）。

    与 :func:`endpoints` 的区别：这里只返回用户真正写下来的键，
    不含从 ``api_base`` 派生的默认值。用于区分「本地显式覆盖」与
    「内置默认」——显式覆盖在与平台宣告域名冲突时应当获胜。
    """
    file_over = _read_endpoints_file(root)
    out: dict[str, str] = {}
    for key in _ENDPOINT_KEYS:
        env_value = os.environ.get(f"TOKENGINE_{key.upper()}")
        value = env_value or file_over.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip().rstrip("/")
    return out


# 模块级常量：环境变量 + 内置默认（不含 endpoints.json，因为它依赖 root）。
# 保留这些名字是为了向后兼容既有代码与测试；需要感知 endpoints.json 的调用方
# 请使用 endpoints(root)。
_EP = endpoints(root=None)

API_BASE = _EP["api_base"]
AUTHORIZE_URL = _EP["authorize_url"]
TOKEN_URL = _EP["token_url"]
USERINFO_URL = _EP["userinfo_url"]
REVOKE_URL = _EP["revoke_url"]
RELAY_BASE = _EP["relay_base"]
STATUS_URL = _EP["status_url"]

# --------------------------------------------------------------------------- #
# 客户端身份
# --------------------------------------------------------------------------- #
CLIENT_ID = os.environ.get("TOKENGINE_CLIENT_ID", "edubuddy-desktop")
SCOPE = os.environ.get("TOKENGINE_SCOPE", "openid relay")

# --------------------------------------------------------------------------- #
# userinfo 字段映射（平台侧命名差异都在这里兜底）
# --------------------------------------------------------------------------- #
# 平台 userinfo 的 "models" 字段名
USERINFO_MODELS_FIELD = os.environ.get("TOKENGINE_USERINFO_MODELS_FIELD", "models")
# 平台 userinfo 的账号显示字段
USERINFO_PHONE_FIELD = os.environ.get("TOKENGINE_USERINFO_PHONE_FIELD", "phone")

# 中继域名候选字段：按顺序取第一个非空值。
# 「登录后拉取域名」就是靠这里——平台在 userinfo 里返回哪个字段都能接住。
USERINFO_RELAY_FIELDS: list[str] = [
    f.strip()
    for f in os.environ.get(
        "TOKENGINE_USERINFO_RELAY_FIELDS",
        "relay_base,base_url,api_base,api_endpoint,endpoint,relay_url,domain",
    ).split(",")
    if f.strip()
]

# 平台返回的域名若不含路径，自动补 /v1（OpenAI 兼容中继的约定）。
RELAY_AUTO_V1 = os.environ.get("TOKENGINE_RELAY_AUTO_V1", "1").lower() not in (
    "0", "false", "no", "off",
)

# 兜底模型：userinfo 没给模型列表时使用
_DEFAULT_MODELS = os.environ.get(
    "TOKENGINE_DEFAULT_MODELS", "deepseek-ai/DeepSeek-V4-Flash-0731"
).split(",")
DEFAULT_MODELS = [m.strip() for m in _DEFAULT_MODELS if m.strip()]

# 登录单次等待上限（默认 15 分钟）
LOGIN_TIMEOUT = int(os.environ.get("TOKENGINE_LOGIN_TIMEOUT", "900"))

# 回调路径（桌面端本地服务），平台回调时必须带上 code + state
CALLBACK_PATH = "/authorize"

# 回环回调监听端口。
#   0  = 交给操作系统分配随机端口（RFC 8252 对原生应用的推荐做法）
#   >0 = 固定端口（排查期有用：平台侧日志/抓包里的回调地址稳定可复现）
#
# 实测（2026-09 对 127.0.0.1:3000）：平台对 redirect_uri 的策略是
# 「字面量 http://127.0.0.1:<任意端口>/<任意路径> 通配」，因此随机端口天然合规，
# 无需为了「地址注册」而固定端口。注意 localhost / [::1] / 无端口一律被拒。
CALLBACK_PORT = int(os.environ.get("TOKENGINE_CALLBACK_PORT", "0"))


# --------------------------------------------------------------------------- #
# 中继域名规整
# --------------------------------------------------------------------------- #
def normalize_relay(value: str) -> str:
    """把平台返回的域名/地址规整成可写入 model_catalog 的 OpenAI 兼容 base_url。

    * 缺 scheme 时补 ``https://``（``tokengine.hanyoai.com`` → ``https://...``）
    * 无路径时补 ``/v1``（可用 ``TOKENGINE_RELAY_AUTO_V1=0`` 关闭）
    * 已有路径（如 ``https://host/api/v1``）原样保留
    """
    text = (value or "").strip().rstrip("/")
    if not text:
        return ""
    if not text.startswith(("http://", "https://")):
        text = "https://" + text
    if RELAY_AUTO_V1:
        try:
            if urllib.parse.urlparse(text).path in ("", "/"):
                text = text + "/v1"
        except ValueError:
            pass
    return text


def pick_relay_from_userinfo(account: dict[str, Any]) -> str:
    """按 USERINFO_RELAY_FIELDS 顺序取平台返回的中继域名，取不到返回空串。"""
    if not isinstance(account, dict):
        return ""
    for field in USERINFO_RELAY_FIELDS:
        value = account.get(field)
        if isinstance(value, str) and value.strip():
            return normalize_relay(value)
    return ""


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def is_loopback(url: str) -> bool:
    """判断地址是否指向本机（联调/自托管场景）。"""
    try:
        host = urllib.parse.urlparse(
            url if "//" in url else "//" + url
        ).hostname or ""
    except ValueError:
        return False
    return host.lower() in _LOOPBACK_HOSTS


# 平台 /api/status 里可能承载对外域名的字段（new-api 系首字段即 server_address）
STATUS_DOMAIN_FIELDS = ("server_address", "serverAddress", "base_url", "domain")


def pick_relay_from_status(payload: dict[str, Any]) -> str:
    """从平台公开状态响应里取「平台对外域名」。

    形如 ``{"success":true,"data":{"server_address":"https://x"}}``；也兼容扁平结构。
    """
    if not isinstance(payload, dict):
        return ""
    candidates: list[dict[str, Any]] = [payload]
    inner = payload.get("data")
    if isinstance(inner, dict):
        candidates.append(inner)
    for scope in candidates:
        for field in STATUS_DOMAIN_FIELDS:
            value = scope.get(field)
            if isinstance(value, str) and value.strip():
                return normalize_relay(value)
    return ""


def resolve_relay(
    api_base: str,
    userinfo: Optional[dict[str, Any]] = None,
    status: Optional[dict[str, Any]] = None,
    fallback: str = "",
    local_override: str = "",
) -> tuple[str, str]:
    """决定最终生效的中继域名，返回 ``(relay_base, source)``。

    优先级（高 → 低）：

    1. **本地显式覆盖**：``endpoints.json`` / 环境变量里写明的 ``relay_base``。
       这是用户/运维最明确的意图声明——测试环境（``tokengine-t``）的
       ``/api/status`` 常从生产克隆、仍宣告生产域名，若让平台宣告压过
       显式配置，外部覆盖文件就形同虚设。
    2. **``api_base`` 是回环地址**（127.0.0.1/localhost）时，以它为准并派生
       ``<api_base>/v1``。理由：联调时平台仍会宣告生产域名 ``server_address``，
       若照抄会把本地中继悄悄指向线上；显式配了回环地址就应当被尊重。
    3. **userinfo 显式字段**（平台未来若直接下发中继地址，这里接得住）。
    4. **平台 /api/status 的 server_address**（运维换域名，客户端自动跟随）。
    5. ``fallback``（本地配置的 relay_base）。

    ``source`` 取值：``local-override`` / ``local-loopback`` / ``userinfo`` /
    ``platform`` / ``local-config``。
    """
    if local_override:
        return local_override, "local-override"

    if api_base and is_loopback(api_base):
        derived = normalize_relay(api_base)
        if derived:
            return derived, "local-loopback"

    pulled = pick_relay_from_userinfo(userinfo or {})
    if pulled:
        return pulled, "userinfo"

    from_status = pick_relay_from_status(status or {})
    if from_status:
        return from_status, "platform"

    if fallback:
        return fallback, "local-config"
    return normalize_relay(api_base), "local-config" if api_base else "unset"
