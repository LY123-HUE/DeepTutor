"""Tokengine 托管实体在 catalog 写入时以 live 为权威（防止陈旧页面快照覆盖登录写入）。"""

from copy import deepcopy

from deeptutor.services.config.tokengine_reconcile import (
    TOKENGINE_CONNECTION_ID,
    reconcile_tokengine_catalog_update,
)


def _live_catalog():
    """桌面端登录写入后的现场：tokengine connection + 各服务 profile。"""
    return {
        "connections": [
            {
                "id": "other",
                "provider": "openai",
                "name": "手动配置",
                "api_key": "user-key",
                "base_url": "https://api.example.com/v1",
            },
            {
                "id": TOKENGINE_CONNECTION_ID,
                "provider": "openai",
                "name": "Tokengine",
                "api_key": "sk-live-token",
                "base_url": "https://relay.example.com/v1",
                "api_version": "",
                "extra_headers": {},
            },
        ],
        "services": {
            "llm": {
                "active_profile_id": "llm-profile-tokengine-abc",
                "active_model_id": "llm-model-1",
                "profiles": [
                    {
                        "id": "llm-profile-manual",
                        "name": "手动 profile",
                        "binding": "custom",
                        "api_key": "user-key",
                        "models": [{"id": "m0", "name": "m0", "model": "m0"}],
                    },
                    {
                        "id": "llm-profile-tokengine-abc",
                        "name": "Tokengine (OpenAI API)",
                        "binding": "custom",
                        "connection_id": TOKENGINE_CONNECTION_ID,
                        "api_key": "sk-live-token",
                        "base_url": "https://relay.example.com/v1",
                        "models": [
                            {"id": "llm-model-1", "name": "对话模型A", "model": "chat-a", "model_type": 1},
                            {"id": "llm-model-2", "name": "对话模型B", "model": "chat-b", "model_type": 1},
                        ],
                    },
                ],
            },
            "embedding": {
                "active_profile_id": None,
                "profiles": [
                    {
                        "id": "emb-profile-tokengine-def",
                        "name": "Tokengine (OpenAI API)",
                        "binding": "custom",
                        "connection_id": TOKENGINE_CONNECTION_ID,
                        "api_key": "sk-live-token",
                        "models": [{"id": "emb-1", "name": "向量模型", "model": "embed-a", "model_type": 5}],
                    }
                ],
            },
        },
    }


def _stale_proposed():
    """登录前浏览器内存里的陈旧快照：无任何 tokengine 实体。"""
    proposed = deepcopy(_live_catalog())
    proposed["connections"] = [
        c for c in proposed["connections"] if c["id"] != TOKENGINE_CONNECTION_ID
    ]
    for service in proposed["services"].values():
        service["profiles"] = [
            p for p in service["profiles"] if p.get("connection_id") != TOKENGINE_CONNECTION_ID
        ]
    proposed["services"]["llm"]["active_profile_id"] = "llm-profile-manual"
    return proposed


def test_stale_snapshot_regains_login_written_entities():
    """覆盖现场：陈旧页面快照 Apply 后，登录写入的 connection/profile 必须回插。"""
    current = _live_catalog()
    proposed = _stale_proposed()
    reconciled = reconcile_tokengine_catalog_update(current, proposed)

    conn_ids = [c["id"] for c in reconciled["connections"]]
    assert TOKENGINE_CONNECTION_ID in conn_ids
    live_conn = next(c for c in reconciled["connections"] if c["id"] == TOKENGINE_CONNECTION_ID)
    assert live_conn["api_key"] == "sk-live-token"
    assert live_conn["base_url"] == "https://relay.example.com/v1"

    llm_profiles = reconciled["services"]["llm"]["profiles"]
    tok = next(p for p in llm_profiles if p.get("connection_id") == TOKENGINE_CONNECTION_ID)
    assert tok["id"] == "llm-profile-tokengine-abc"
    assert [m["model"] for m in tok["models"]] == ["chat-a", "chat-b"]
    assert tok["api_key"] == "sk-live-token"

    emb = reconciled["services"]["embedding"]["profiles"]
    assert any(p.get("connection_id") == TOKENGINE_CONNECTION_ID for p in emb)
    # 手动配置的实体不受影响
    assert any(p["id"] == "llm-profile-manual" for p in llm_profiles)


def test_edited_credentials_and_models_revert_to_live():
    """proposed 携带被清空/篡改的托管实体时，凭据与模型以 live 为权威。"""
    current = _live_catalog()
    proposed = deepcopy(current)
    for service in proposed["services"].values():
        for profile in service["profiles"]:
            if profile.get("connection_id") == TOKENGINE_CONNECTION_ID:
                profile["models"] = []
                profile["api_key"] = ""
                profile["base_url"] = "https://api.openai.com/v1"
                profile["name"] = "我改的名字"
    tok_conn = next(c for c in proposed["connections"] if c["id"] == TOKENGINE_CONNECTION_ID)
    tok_conn["api_key"] = ""
    tok_conn["base_url"] = "https://api.openai.com/v1"

    reconciled = reconcile_tokengine_catalog_update(current, proposed)
    live_conn = next(c for c in reconciled["connections"] if c["id"] == TOKENGINE_CONNECTION_ID)
    assert live_conn["api_key"] == "sk-live-token"
    assert live_conn["base_url"] == "https://relay.example.com/v1"

    tok = next(
        p
        for p in reconciled["services"]["llm"]["profiles"]
        if p.get("connection_id") == TOKENGINE_CONNECTION_ID
    )
    assert [m["model"] for m in tok["models"]] == ["chat-a", "chat-b"]
    assert tok["api_key"] == "sk-live-token"
    assert tok["base_url"] == "https://relay.example.com/v1"
    # 显示名是设置页可编辑字段：透传 proposed
    assert tok["name"] == "我改的名字"


def test_169_provider_ref_profile_is_reconciled():
    """1.6.9 只写 provider_ref 引用时，仍按登录托管实体保护。"""
    current = _live_catalog()
    live_profile = current["services"]["llm"]["profiles"][1]
    live_profile.pop("connection_id")
    live_profile["provider_ref"] = {
        "connection_id": TOKENGINE_CONNECTION_ID,
        "binding": "openai",
        "default_base_url": "https://api.openai.com/v1",
    }
    proposed = deepcopy(current)
    proposed["services"]["llm"]["profiles"][1]["api_key"] = ""
    proposed["services"]["llm"]["profiles"][1]["models"] = []

    reconciled = reconcile_tokengine_catalog_update(current, proposed)
    profile = reconciled["services"]["llm"]["profiles"][1]

    assert profile["id"] == live_profile["id"]
    assert profile["api_key"] == "sk-live-token"
    assert [item["model"] for item in profile["models"]] == ["chat-a", "chat-b"]
    assert profile["provider_ref"]["connection_id"] == TOKENGINE_CONNECTION_ID


def test_stale_entities_dropped_after_logout():
    """退出登录（live 已摘除）后，陈旧快照不得借 Apply 复活已吊销的令牌。"""
    current = {"connections": [], "services": {"llm": {"profiles": []}}}
    proposed = deepcopy(_live_catalog())
    reconciled = reconcile_tokengine_catalog_update(current, proposed)
    assert all(c["id"] != TOKENGINE_CONNECTION_ID for c in reconciled["connections"])
    for service in reconciled["services"].values():
        assert all(
            p.get("connection_id") != TOKENGINE_CONNECTION_ID for p in service["profiles"]
        )


def test_non_tokengine_entities_pass_through_when_live_empty():
    """live 为空时，proposed 里的非托管实体原样放行，不误伤。"""
    current = {"connections": [], "services": {}}
    proposed = _live_catalog()
    reconciled = reconcile_tokengine_catalog_update(current, proposed)

    # 手动配置的 connection/profile 不受影响
    assert any(c["id"] == "other" for c in reconciled["connections"])
    assert any(
        p["id"] == "llm-profile-manual"
        for p in reconciled["services"]["llm"]["profiles"]
    )
    # "tokengine" 是桌面登录保留 id：live 没有说明未登录/已退出，
    # proposed 携带的托管实体一律丢弃（防止吊销令牌借 Apply 复活）
    assert all(c["id"] != TOKENGINE_CONNECTION_ID for c in reconciled["connections"])


def test_non_tokengine_services_untouched():
    """search/tts/stt 等服务不在托管范围，proposed 原样保留。"""
    current = {"connections": [], "services": {}}
    proposed = {
        "connections": [],
        "services": {
            "search": {"profiles": [{"id": "s1", "provider": "tavily"}]},
            "tts": {"profiles": [{"id": "t1", "connection_id": TOKENGINE_CONNECTION_ID}]},
        },
    }
    reconciled = reconcile_tokengine_catalog_update(current, proposed)
    assert reconciled["services"]["search"]["profiles"][0]["id"] == "s1"
    # tts 不在分拣服务名单：connection_id 匹配也不动
    assert reconciled["services"]["tts"]["profiles"][0] == {"id": "t1", "connection_id": TOKENGINE_CONNECTION_ID}
