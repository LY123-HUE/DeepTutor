"""DeepTutor model catalog 的精细写入：把业务令牌注入活动 LLM 连接。

目标文件：<home>/data/user/settings/model_catalog.json
策略（不强改、不重建，只做最小手术）：
  1. 保留用户已有 catalog；
  2. 确保存在一条 id="tokengine" 的 connection（api_key/ base_url 同步）；
  3. 复用/创建 services.llm 的活动 profile，把 api_key 换成新令牌，
     base_url 对齐中继地址，并按需覆盖 models 列表；
  4. 若平台返回了模型列表则整体替换模型（首模型设为活动），否则保留现值。
写入用「临时文件 + os.replace」保证原子性，格式与 DeepTutor 自身的
ModelCatalogService.save 一致，加载时会自动 normalize。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("dt.auth.catalog")

# 与 DeepTutor ModelCatalogService 的空壳保持一致
_SERVICE_NAMES = ("llm", "task", "embedding", "search", "tts", "stt", "imagegen", "videogen")


def _service_shell() -> dict[str, Any]:
    return {"active_profile_id": None, "active_model_id": None, "profiles": []}


def _search_shell() -> dict[str, Any]:
    return {"active_profile_id": None, "profiles": []}


def _default_catalog() -> dict[str, Any]:
    return {
        "version": 1,
        "connections": [],
        "services": {name: _search_shell() if name == "search" else _service_shell()
                     for name in _SERVICE_NAMES},
    }


def _atomic_write_json(path: Path, catalog: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(catalog, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def _load_catalog(path: Path) -> dict[str, Any]:
    if path.exists() and path.stat().st_size > 0:
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                base = _default_catalog()
                base.update({k: v for k, v in loaded.items() if k != "services"})
                base["services"].update(loaded.get("services", {}))
                return base
        except (OSError, ValueError):
            log.warning("model_catalog 解析失败，使用默认结构重建")
    return _default_catalog()


def _model_entries(models: list[str]) -> list[dict[str, str]]:
    ts = int(time.time() * 1000)
    return [
        {"id": f"llm-model-{ts}-{idx}", "name": m, "model": m}
        for idx, m in enumerate(models)
    ]


def catalog_path(home: Path) -> Path:
    return home / "data" / "user" / "settings" / "model_catalog.json"


def remove_tokengine_catalog(home: Path, token: str = "") -> Path:
    """把登录流程写入的 Tokengine 配置从 model_catalog 摘除（退出登录用）。

    只摘除"我们写进去的"东西，绝不误伤用户手动配置：

      1. ``id == 'tokengine'`` 的 connection —— 登录时新建/刷新的那条；
      2. ``connection_id == 'tokengine'`` 的 llm profile —— 首次登录时我们
         新建的活动 profile，整条移除；
      3. 被登录流程**复用**的既存活动 profile —— 仅当它的 ``api_key`` 与
         刚吊销的 ``token`` 完全一致才清空凭据（api_key/models）。
         其余一律保留：那多半是用户自己配的连接，退出登录不该动它。

    返回被（可能）修改的文件路径；无任何变化时返回同一路径、不落盘。
    """
    path = catalog_path(home)
    catalog = _load_catalog(path)
    dirty = False

    # 1) connection
    conns = list(catalog.get("connections") or [])
    if any(isinstance(c, dict) and c.get("id") == "tokengine" for c in conns):
        catalog["connections"] = [c for c in conns
                                  if not (isinstance(c, dict) and c.get("id") == "tokengine")]
        dirty = True

    # 2)+3) llm profiles
    llm = catalog.setdefault("services", {}).setdefault("llm", _service_shell())
    profiles = list(llm.get("profiles") or [])
    kept: list[dict[str, Any]] = []
    for p in profiles:
        if not isinstance(p, dict):
            kept.append(p)          # 异常结构绝不碰
            continue
        if p.get("connection_id") == "tokengine":
            dirty = True            # 我们建的，整条移除
            continue
        if token and p.get("api_key") == token:
            p["api_key"] = ""
            p["models"] = []        # 复用型 profile：只清我们写过的字段
            dirty = True
        kept.append(p)
    llm["profiles"] = kept

    # 4) active 指针修正：被删的 profile 不再指向
    active = llm.get("active_profile_id")
    if active and not any(isinstance(p, dict) and p.get("id") == active for p in kept):
        nxt = next((p.get("id") for p in kept
                    if isinstance(p, dict) and p.get("api_key")), None)
        llm["active_profile_id"] = nxt or None
        dirty = True
    if not llm.get("active_profile_id") and kept:
        first = next((p for p in kept if isinstance(p, dict)), None)
        if first and first.get("id"):
            llm["active_profile_id"] = first["id"]
            dirty = True

    if dirty:
        _atomic_write_json(path, catalog)
        log.info("tokengine catalog detached from %s", path)
    return path


def has_configured_token(home: Path) -> bool:
    """活动 LLM profile 是否已有一条 api_key（手工配置或之前登录写入的）。"""
    path = catalog_path(home)
    if not path.exists():
        return False
    catalog = _load_catalog(path)
    profile = _active_llm_profile(catalog)
    return bool(profile and str(profile.get("api_key") or "").strip())


def _active_llm_profile(catalog: dict[str, Any]) -> Optional[dict[str, Any]]:
    llm = catalog.get("services", {}).get("llm") or {}
    profiles = llm.get("profiles") or []
    active_id = llm.get("active_profile_id")
    for profile in profiles:
        if isinstance(profile, dict) and profile.get("id") == active_id:
            return profile
    return profiles[0] if profiles else None


def ensure_tokengine_catalog(
    home: Path,
    api_key: str,
    base_url: str,
    models: Optional[list[str]] = None,
    connection_id: str = "tokengine",
) -> Path:
    """把业务令牌写入 DeepTutor model catalog，返回被修改的文件路径。"""
    path = catalog_path(home)
    catalog = _load_catalog(path)

    # 1) connection：存在则刷新凭据，否则新建
    connections = catalog.setdefault("connections", [])
    conn = next((c for c in connections if c.get("id") == connection_id), None)
    if conn is None:
        conn = {
            "id": connection_id,
            "provider": "openai",
            "name": "Tokengine",
            "api_key": api_key,
            "base_url": base_url,
            "api_version": "",
            "extra_headers": {},
        }
        connections.append(conn)
    else:
        conn["api_key"] = api_key
        if base_url:
            conn["base_url"] = base_url

    # 2) services.llm 活动 profile：复用现有活动连接（保留 binding/模型等）
    llm = catalog["services"].setdefault("llm", _service_shell())
    profiles = llm.setdefault("profiles", [])
    profile = _active_llm_profile(catalog)
    if profile is None:
        profile = {
            "id": f"llm-profile-tokengine-{uuid.uuid4().hex[:8]}",
            "name": "Tokengine (OpenAI API)",
            "binding": "custom",
            "base_url": base_url,
            "api_key": api_key,
            "api_version": "",
            "extra_headers": {},
            "wire_api": "auto",
            "api_format": "auto",
            "models": [],
            "connection_id": connection_id,
        }
        profiles.append(profile)
    else:
        profile["api_key"] = api_key
        if base_url:
            profile["base_url"] = base_url

    # 3) 模型列表：平台给了就替换，没给就保留
    if models:
        profile["models"] = _model_entries(models)
        llm["active_model_id"] = profile["models"][0]["id"]
    elif not llm.get("active_model_id") and profile.get("models"):
        first = (profile["models"] or [{}])[0]
        llm["active_model_id"] = first.get("id")

    if not llm.get("active_profile_id"):
        llm["active_profile_id"] = profile["id"]

    _atomic_write_json(path, catalog)
    log.info("model catalog updated with Tokengine token -> %s", path)
    return path
