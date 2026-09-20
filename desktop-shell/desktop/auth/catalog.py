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


def _model_entries(
    entries: "list[dict[str, Any]] | list[str]",
    prefix: str = "llm",
) -> list[dict[str, Any]]:
    """Build catalog model rows from split entries.

    Accepts either plain model-name strings (legacy) or
    ``{"name", "model_type"}`` dicts produced by
    :func:`split_models_by_service`. The platform ``model_type`` is persisted
    onto each row so DeepTutor's services/UI have an authoritative type tag
    instead of re-guessing from the name (a chat picker must never offer an
    embedding/rerank model that merely lost its tag in transit).
    """
    ts = int(time.time() * 1000)
    rows: list[dict[str, Any]] = []
    for idx, entry in enumerate(entries):
        if isinstance(entry, dict):
            name = str(entry.get("name") or "").strip()
            model_type = entry.get("model_type")
        else:
            name = str(entry).strip()
            model_type = None
        if not name:
            continue
        row: dict[str, Any] = {
            "id": f"{prefix}-model-{ts}-{idx}",
            "name": name,
            "model": name,
        }
        if isinstance(model_type, int) and not isinstance(model_type, bool):
            row["model_type"] = model_type
        rows.append(row)
    return rows


# 平台 /oauth/userinfo 下发的 model_type 枚举（与模型管理后台
# 「模型类型」下拉一致，oauth_provider.go 注释同步维护）：
#   1=文生文 2=文生图 3=文生视频 4=重排序 5=向量；0/缺失=未标注。
_MT_LLM = 1
_MT_IMAGEGEN = 2
_MT_VIDEOGEN = 3
_MT_RERANK = 4          # DeepTutor 无重排服务：平台明确标注时确定性丢弃
_MT_EMBEDDING = 5

# model_type -> DeepTutor 服务名（不在表内 = 该类型桌面端不自动绑定）
_MODEL_TYPE_SERVICE: dict[int, str] = {
    _MT_LLM: "llm",
    _MT_IMAGEGEN: "imagegen",
    _MT_VIDEOGEN: "videogen",
    _MT_EMBEDDING: "embedding",
}

# 服务名 -> model_type 反推：名称启发式归类后仍给每条盖上权威类型章。
# task 是对话模型的双挂载服务，与 llm 同为文生文（1）。
_SERVICE_MODEL_TYPE: dict[str, int] = {
    "llm": _MT_LLM,
    "task": _MT_LLM,
    "imagegen": _MT_IMAGEGEN,
    "videogen": _MT_VIDEOGEN,
    "embedding": _MT_EMBEDDING,
}

# 除 llm 外由登录流程自动挂 tokengine profile 的服务
_TYPED_SERVICES = ("task", "embedding", "imagegen", "videogen")


def _normalized_model_type(mt: Any) -> Optional[int]:
    """把平台下发的 model_type 归一成 int；无法解析返回 None（视同未标注）。"""
    try:
        return int(mt)
    except (TypeError, ValueError):
        return None


def split_models_by_service(
    models: list[str],
    model_types: Optional[dict[str, Any]] = None,
) -> dict[str, list[dict[str, Any]]]:
    """把 userinfo 授权的模型名列表按平台 ``model_type`` 分拣到各服务。

    纯 model_type 分流，不做名称启发式兜底：
      1=文生文 → llm + task
      2=文生图 → imagegen
      3=文生视频 → videogen
      4=重排序 → 丢弃（DeepTutor 无重排服务）
      5=向量 → embedding
      0/缺失/未知 → 保守归对话（llm + task）

    对话类型（llm）同时复制进 task（文生文对话服务双挂载）。
    每条都带确定的 model_type（见 ``_SERVICE_MODEL_TYPE``），供
    ``_model_entries`` 落库。
    """
    types = model_types if isinstance(model_types, dict) else {}
    seen: set[str] = set()
    out: dict[str, list[dict[str, Any]]] = {
        "llm": [], **{s: [] for s in _TYPED_SERVICES}
    }

    def _emit(service: str, name: str, mt: Optional[int]) -> None:
        authoritative = (
            isinstance(mt, int) and not isinstance(mt, bool)
            and mt in _MODEL_TYPE_SERVICE
        )
        resolved_mt = mt if authoritative else _SERVICE_MODEL_TYPE.get(service)
        entry = {"name": name}
        if isinstance(resolved_mt, int):
            entry["model_type"] = resolved_mt
        out[service].append(entry)

    for raw in models or []:
        name = str(raw).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        mt = _normalized_model_type(types.get(name)) if name in types else None

        if mt == _MT_RERANK:
            continue

        if mt == _MT_EMBEDDING:
            _emit("embedding", name, _MT_EMBEDDING)
        elif mt == _MT_VIDEOGEN:
            _emit("videogen", name, _MT_VIDEOGEN)
        elif mt == _MT_IMAGEGEN:
            _emit("imagegen", name, _MT_IMAGEGEN)
        else:
            _emit("llm", name, mt if mt == _MT_LLM else None)
            _emit("task", name, _MT_LLM)
    return out


def _remove_tokengine_profile(
    catalog: dict[str, Any],
    service_name: str,
    connection_id: str,
) -> None:
    """从某服务摘除我们写的 tokengine profile（该服务授权模型为空时用）。

    不然上一次登录（或网关全量名单）写入的模型会一直残留——刷新的语义
    是「以平台授权为准重算」，空的授权就该对应空的 tokengine profile。
    用户自己的 profile（connection_id 不匹配）一律不碰。
    """
    service = (catalog.get("services") or {}).get(service_name)
    if not isinstance(service, dict):
        return
    profiles = list(service.get("profiles") or [])
    kept = [p for p in profiles
            if not (isinstance(p, dict) and p.get("connection_id") == connection_id)]
    if len(kept) == len(profiles):
        return                          # 没有我们的 profile，无事可做
    service["profiles"] = kept
    active = service.get("active_profile_id")
    if active and not any(isinstance(p, dict) and p.get("id") == active
                          for p in kept):
        nxt = next((p.get("id") for p in kept
                    if isinstance(p, dict) and p.get("api_key")), None)
        service["active_profile_id"] = nxt or None
        if not nxt:
            service["active_model_id"] = None


def _upsert_tokengine_profile(
    catalog: dict[str, Any],
    service_name: str,
    connection_id: str,
    base_url: str,
    api_key: str,
    model_entries: "list[dict[str, Any]] | list[str]",
) -> None:
    """在某服务下创建/刷新 tokengine profile（只动我们自己写的那条）。

    * 复用 ``connection_id`` 匹配的既有 profile，刷新凭据与模型列表；
      没有才新建——绝不碰用户手动配置的其他 profile；
    * 活动 profile 指针只在服务当前没有活动 profile 时指向我们的；
      我们自己的就是活动 profile 时，模型列表更换后活动模型跟着指到
      第一个（与 llm 服务的行为一致）。

    ``model_entries`` 为 :func:`split_models_by_service` 产出的
    ``[{"name", "model_type"}]``（也兼容纯模型名字符串）。
    """
    service = catalog.setdefault("services", {}).setdefault(
        service_name, _service_shell())
    profiles = service.setdefault("profiles", [])
    profile = next(
        (p for p in profiles
         if isinstance(p, dict) and p.get("connection_id") == connection_id),
        None,
    )
    if profile is None:
        profile = {
            "id": f"{service_name}-profile-tokengine-{uuid.uuid4().hex[:8]}",
            "name": "Tokengine (OpenAI API)",
            "binding": "custom",
            "base_url": base_url,
            "api_key": api_key,
            "api_version": "",
            "extra_headers": {},
            "models": [],
            "connection_id": connection_id,
        }
        profiles.append(profile)
    else:
        profile["api_key"] = api_key
        if base_url:
            profile["base_url"] = base_url

    if not model_entries:
        return                      # 平台无此类型模型：保留 profile 现状
    profile["models"] = _model_entries(model_entries, service_name)
    if not service.get("active_profile_id"):
        service["active_profile_id"] = profile["id"]
        service["active_model_id"] = profile["models"][0]["id"]
    elif service.get("active_profile_id") == profile["id"]:
        active = service.get("active_model_id")
        if not any(m.get("id") == active for m in profile["models"]):
            service["active_model_id"] = profile["models"][0]["id"]


def catalog_path(home: Path) -> Path:
    return home / "data" / "user" / "settings" / "model_catalog.json"


def remove_tokengine_catalog(home: Path, token: str = "") -> Path:
    """把登录流程写入的 Tokengine 配置从 model_catalog 摘除（退出登录用）。

    只摘除"我们写进去的"东西，绝不误伤用户手动配置：

      1. ``id == 'tokengine'`` 的 connection —— 登录时新建/刷新的那条；
      2. **所有模型服务**里 ``connection_id == 'tokengine'`` 的 profile
         —— 登录时新建/刷新的那条（登录现在会往 llm/task/embedding/
         imagegen/videogen 各挂一条）；
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

    # 2)+3) 各模型服务的 profile（llm + 登录分拣写入的 task/embedding/…）
    services = catalog.setdefault("services", {})
    for service_name in _SERVICE_NAMES:
        if service_name == "search":
            continue            # search 走独立 provider 结构，tokengine 不写入
        service = services.setdefault(service_name, _service_shell())
        profiles = list(service.get("profiles") or [])
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
        service["profiles"] = kept

        # 4) active 指针修正：被删的 profile 不再指向
        active = service.get("active_profile_id")
        if active and not any(isinstance(p, dict) and p.get("id") == active
                              for p in kept):
            nxt = next((p.get("id") for p in kept
                        if isinstance(p, dict) and p.get("api_key")), None)
            service["active_profile_id"] = nxt or None
            dirty = True
        if not service.get("active_profile_id") and kept:
            first = next((p for p in kept if isinstance(p, dict)), None)
            if first and first.get("id"):
                service["active_profile_id"] = first["id"]
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
    model_types: Optional[dict[str, Any]] = None,
) -> Path:
    """把业务令牌写入 DeepTutor model catalog，返回被修改的文件路径。

    ``models``：平台 userinfo 返回的授权模型名（登录实测 8 个）——
    它是**唯一**名单来源，绝不请求网关 /v1/models（那是全量列表，
    会把设置页撑爆）。

    ``model_types``：userinfo 同步下发的 名称->model_type 映射
    （1=文生文 2=文生图 3=文生视频 4=重排序 5=向量）——分流的**权威**
    依据；缺失/未标注的模型回退名称启发式。分流结果：对话模型进
    llm/task，向量模型进 embedding，图像/视频各归其位，重排序丢弃；
    某服务授权为空时摘除我们之前写的 profile。

    models 未传（None）时不碰模型列表（如端点对齐路径）。
    """
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

    # 3) 模型列表：models（userinfo 授权名单）是唯一来源，按平台下发的
    #    model_type 分流（缺类型时回退名称启发式）到各服务；
    #    models=None（端点对齐等零网络路径）不碰列表。
    split = (split_models_by_service(models, model_types)
             if models is not None else None)
    if split is not None and split["llm"]:
        profile["models"] = _model_entries(split["llm"])
        llm["active_model_id"] = profile["models"][0]["id"]
    elif split is not None:
        # 授权名单里没有对话类型模型：llm 留空并清掉活动指针（诚实呈现，
        # 绝不把向量/图像模型混进对话列表——那正是本次要修的污染）
        profile["models"] = []
        llm["active_model_id"] = None
        log.warning("授权模型中无对话类型，llm 服务留空")
    elif models:
        profile["models"] = _model_entries(models)
        llm["active_model_id"] = profile["models"][0]["id"]
    elif not llm.get("active_model_id") and profile.get("models"):
        first = (profile["models"] or [{}])[0]
        llm["active_model_id"] = first.get("id")

    # 3.5) task / embedding / imagegen / videogen：按名称分流各挂一条
    #      tokengine profile；该类型授权为空时摘除我们之前写的 profile，
    #      避免上一次（可能超授权的）名单残留。
    if split is not None:
        for service_name in _TYPED_SERVICES:
            if split.get(service_name):
                _upsert_tokengine_profile(
                    catalog, service_name, connection_id, base_url,
                    api_key, split[service_name])
            else:
                _remove_tokengine_profile(catalog, service_name, connection_id)

    if not llm.get("active_profile_id"):
        llm["active_profile_id"] = profile["id"]

    _atomic_write_json(path, catalog)
    log.info("model catalog updated with Tokengine token -> %s", path)
    return path
