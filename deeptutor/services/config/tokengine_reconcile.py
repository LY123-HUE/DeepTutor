"""Tokengine 托管实体的 catalog 写入保护。

桌面端登录 Tokengine 平台后，``desktop-shell/desktop/auth/catalog.py``
会把 ``/oauth/userinfo`` 下发的授权模型按 ``model_type`` 分拣写入
model catalog：

  - ``id == "tokengine"`` 的 connection（携带 sk- 业务令牌）；
  - llm/task/embedding/imagegen/videogen 各服务里
    ``connection_id == "tokengine"`` 的 profile（模型名单唯一来源是
    userinfo，绝不请求网关 /v1/models 全量列表）。

设置页的整包写入（PUT /catalog、POST /apply、POST /apply/registry、
POST /apply/provider、POST /apply/service）都携带浏览器内存中的 catalog
快照 —— 若该快照早于登录，就会把桌面端刚写入的连接与模型整体覆盖掉
（典型现场：登录 96 秒后点一次 Apply，模型列表清空）。

本模块与 ``reconcile_codex_catalog_update`` 同构，在每次 catalog 写入前
以 live（磁盘当前值）为权威对齐 tokengine 托管实体：

  - live 有、proposed 无 → 从 live 回插（陈旧页面快照不含登录写入时，
    Apply 不得删掉它们 —— 即本次修复的覆盖现场）；
  - live 有、proposed 有 → 以 live 重建，仅透传设置页可编辑的展示字段
    （显示名）；
  - live 无、proposed 有 → 丢弃（已退出登录时，防止已吊销的令牌借
    Apply 复活；与桌面端 remove_tokengine_catalog 语义一致）。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

# 与桌面端写入约定一致的托管标记（desktop/auth/catalog.py: connection_id）。
TOKENGINE_CONNECTION_ID = "tokengine"

# live 有、proposed 有时，允许从 proposed 覆盖 live 的字段：都是设置页
# 可直接编辑的展示字段。其余（api_key / base_url / models / binding /
# connection_id 等）一律以 live 为权威 —— 由登录流程维护。
_CONNECTION_EDITABLE_FIELDS = ("name",)
_PROFILE_EDITABLE_FIELDS = ("name", "user_name")

# 桌面端按 model_type 分拣写入的服务（与 _TYPED_SERVICES + llm 一致；
# search 走独立 provider 结构，tokengine 不写入）。
_TOKENGINE_SERVICES = ("llm", "task", "embedding", "imagegen", "videogen")


def _live_connection(current: Mapping[str, Any]) -> dict[str, Any] | None:
    conns = current.get("connections")
    if not isinstance(conns, list):
        return None
    for conn in conns:
        if isinstance(conn, Mapping) and conn.get("id") == TOKENGINE_CONNECTION_ID:
            return deepcopy(dict(conn))
    return None


def _live_profiles(
    current: Mapping[str, Any], service_name: str
) -> dict[str, dict[str, Any]]:
    service = (current.get("services") or {}).get(service_name)
    profiles = service.get("profiles") if isinstance(service, Mapping) else None
    if not isinstance(profiles, list):
        return {}
    return {
        str(p.get("id")): deepcopy(dict(p))
        for p in profiles
        if _is_tokengine_profile(p) and p.get("id")
    }


def _profile_connection_id(profile: Mapping[str, Any]) -> str:
    """Read DeepTutor 1.6.9's linked-provider reference when available."""
    connection_id = str(profile.get("connection_id") or "")
    if connection_id:
        return connection_id
    ref = profile.get("provider_ref")
    if isinstance(ref, Mapping):
        return str(ref.get("connection_id") or "")
    return ""


def _is_tokengine_profile(profile: Mapping[str, Any]) -> bool:
    return _profile_connection_id(profile) == TOKENGINE_CONNECTION_ID


def _reconcile_connection(
    reconciled: dict[str, Any],
    current: Mapping[str, Any],
) -> None:
    live = _live_connection(current)
    connections = reconciled.get("connections")
    if not isinstance(connections, list):
        if live is None:
            return  # 双方都没有 tokengine 实体：proposed 原样保留
        connections = []
    kept: list[Any] = []
    changed = False
    for conn in connections:
        if isinstance(conn, dict) and conn.get("id") == TOKENGINE_CONNECTION_ID:
            changed = True
            if live is None:
                continue  # live 已退出登录：丢弃 proposed 里的陈旧实体
            restored = deepcopy(live)
            for field in _CONNECTION_EDITABLE_FIELDS:
                if field in conn:
                    restored[field] = conn[field]
            kept.append(restored)
        else:
            kept.append(conn)
    if live is not None and not any(
        isinstance(c, dict) and c.get("id") == TOKENGINE_CONNECTION_ID for c in kept
    ):
        # live 有、proposed 无（陈旧页面快照）：回插，保住登录写入
        kept.append(deepcopy(live))
        changed = True
    if changed:
        reconciled["connections"] = kept


def _reconcile_service(
    reconciled: dict[str, Any],
    current: Mapping[str, Any],
    service_name: str,
) -> None:
    live_by_id = _live_profiles(current, service_name)
    services = reconciled.get("services")
    if not isinstance(services, dict):
        return
    service = services.get(service_name)
    if not isinstance(service, dict):
        return
    profiles = service.get("profiles")
    if not isinstance(profiles, list):
        return
    kept: list[Any] = []
    changed = False
    for profile in profiles:
        if isinstance(profile, dict) and _is_tokengine_profile(profile):
            live = live_by_id.get(str(profile.get("id")))
            if live is None:
                changed = True
                continue  # live 已退出登录（或 id 不符）：丢弃陈旧实体
            restored = deepcopy(live)
            for field in _PROFILE_EDITABLE_FIELDS:
                if field in profile:
                    restored[field] = profile[field]
            if restored != profile:
                changed = True
            kept.append(restored)
        else:
            kept.append(profile)
    # live 有、proposed 无：按 live 顺序回插缺失的托管 profile
    kept_ids = {
        p.get("id") for p in kept if isinstance(p, dict)
    }
    for profile_id, live in live_by_id.items():
        if profile_id not in kept_ids:
            kept.append(deepcopy(live))
            changed = True
    if changed:
        service["profiles"] = kept


def reconcile_tokengine_catalog_update(
    current_catalog: Mapping[str, Any],
    proposed_catalog: Mapping[str, Any],
) -> dict[str, Any]:
    """以 live 为权威对齐 tokengine 托管的 connection 与各服务 profile。"""
    reconciled = deepcopy(dict(proposed_catalog))
    _reconcile_connection(reconciled, current_catalog)
    for service_name in _TOKENGINE_SERVICES:
        _reconcile_service(reconciled, current_catalog, service_name)
    return reconciled


__all__ = ["TOKENGINE_CONNECTION_ID", "reconcile_tokengine_catalog_update"]
