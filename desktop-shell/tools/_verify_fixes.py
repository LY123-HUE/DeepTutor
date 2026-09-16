# -*- coding: utf-8 -*-
"""验证两处修复的快速自检（不依赖网络，用临时目录模拟真实数据流）。"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from desktop.auth import config as cfg
from desktop.auth.manager import AuthManager

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail else ""))
    if not cond:
        failures.append(name)


# ------------------------------------------------------------------ #
# 1) resolve_relay：显式覆盖必须压过平台宣告（问题 2 的核心）
# ------------------------------------------------------------------ #
relay, src = cfg.resolve_relay(
    api_base="https://tokengine-t.hanyoai.com",
    status={"data": {"server_address": "https://tokengine.hanyoai.com"}},
    fallback="https://tokengine-t.hanyoai.com/v1",
    local_override="https://tokengine-t.hanyoai.com/v1",
)
check("显式 relay_base 压过平台宣告", relay == "https://tokengine-t.hanyoai.com/v1"
      and src == "local-override", f"{relay} ({src})")

relay, src = cfg.resolve_relay(
    api_base="https://tokengine.hanyoai.com",
    status={"data": {"server_address": "https://tokengine.hanyoai.com"}},
    fallback="https://tokengine.hanyoai.com/v1",
)
check("无显式覆盖时维持旧行为（平台宣告）", relay == "https://tokengine.hanyoai.com/v1"
      and src == "platform", f"{relay} ({src})")

relay, src = cfg.resolve_relay(api_base="http://127.0.0.1:3000", local_override="")
check("回环 api_base 仍走 local-loopback", relay == "http://127.0.0.1:3000/v1"
      and src == "local-loopback", f"{relay} ({src})")

relay, src = cfg.resolve_relay(
    api_base="https://tokengine.hanyoai.com",
    userinfo={"domain": "relay.example.com"},
    local_override="",
)
check("userinfo 字段仍生效（无覆盖时）", relay == "https://relay.example.com/v1"
      and src == "userinfo", f"{relay} ({src})")

# ------------------------------------------------------------------ #
# 2) explicit_overrides：env > 文件，且只含显式键
# ------------------------------------------------------------------ #
import os
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    (root / "endpoints.json").write_text(json.dumps({
        "api_base": "https://tokengine-t.hanyoai.com",
        "relay_base": "https://tokengine-t.hanyoai.com/v1",
    }), encoding="utf-8")
    ov = cfg.explicit_overrides(root)
    check("文件覆盖被收集", ov.get("relay_base") == "https://tokengine-t.hanyoai.com/v1"
          and ov.get("api_base") == "https://tokengine-t.hanyoai.com", str(ov))
    os.environ["TOKENGINE_RELAY_BASE"] = "https://env-win.example/v1"
    ov2 = cfg.explicit_overrides(root)
    check("env 优先于文件", ov2.get("relay_base") == "https://env-win.example/v1", str(ov2))
    os.environ.pop("TOKENGINE_RELAY_BASE")
    empty = cfg.explicit_overrides(Path(td) / "nonexist")
    check("无文件时返回空", empty == {}, str(empty))

# ------------------------------------------------------------------ #
# 3) apply_endpoint_overrides：启动对齐重写 catalog + auth store
# ------------------------------------------------------------------ #
with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "appdata"   # TokenStore / endpoints.json 所在
    home = Path(td) / "workspace"  # model_catalog 所在
    root.mkdir()
    (root / "endpoints.json").write_text(json.dumps({
        "api_base": "https://tokengine-t.hanyoai.com",
        "relay_base": "https://tokengine-t.hanyoai.com/v1",
    }), encoding="utf-8")

    mgr = AuthManager(root=root, home=home)
    # 未登录 -> 不动
    r = mgr.apply_endpoint_overrides()
    check("未登录时跳过", r.get("changed") is False and r.get("reason") == "not_logged_in")

    # 模拟已登录（旧绑定指向生产）
    store_payload = {
        "token": "sk-test-token",
        "access_token": "at",
        "refresh_token": "",
        "expires_at": 0,
        "account": {"phone": "15512348602",
                    "models": ["deepseek-ai/DeepSeek-V4-Flash-0731"],
                    "balance": 16.77},
        "relay_base": "https://tokengine.hanyoai.com/v1",
        "relay_source": "platform",
    }
    mgr._store.save(store_payload)
    from desktop.auth.catalog import ensure_tokengine_catalog
    ensure_tokengine_catalog(home=home, api_key="sk-test-token",
                             base_url="https://tokengine.hanyoai.com/v1",
                             models=["deepseek-ai/DeepSeek-V4-Flash-0731"])

    r = mgr.apply_endpoint_overrides()
    check("启动对齐执行重绑定", r.get("changed") is True
          and r.get("relay_base") == "https://tokengine-t.hanyoai.com/v1"
          and r.get("previous") == "https://tokengine.hanyoai.com/v1", str(r))

    cat = json.loads((home / "data/user/settings/model_catalog.json").read_text(encoding="utf-8"))
    conn = next(c for c in cat["connections"] if c["id"] == "tokengine")
    prof = next(p for p in cat["services"]["llm"]["profiles"]
                if p["connection_id"] == "tokengine")
    check("catalog connection 已指向测试环境",
          conn["base_url"] == "https://tokengine-t.hanyoai.com/v1", conn["base_url"])
    check("catalog profile 已指向测试环境",
          prof["base_url"] == "https://tokengine-t.hanyoai.com/v1", prof["base_url"])
    check("模型列表保留", len(prof["models"]) == 1, str(len(prof["models"])))

    payload = mgr._store.load()
    check("auth store 回写 relay_base/source",
          payload["relay_base"] == "https://tokengine-t.hanyoai.com/v1"
          and payload["relay_source"] == "local-override",
          f"{payload['relay_base']} ({payload['relay_source']})")

    # 幂等：再跑一次应 no-op
    r2 = mgr.apply_endpoint_overrides()
    check("重复执行幂等（already_in_sync）", r2.get("changed") is False
          and r2.get("reason") == "already_in_sync", str(r2))

    # status() 现在应返回新地址（菜单「复制 API 地址」用）
    st = mgr.status()
    check("status() 反映新中继", st["relay_base"] == "https://tokengine-t.hanyoai.com/v1",
          st["relay_base"])

# ------------------------------------------------------------------ #
# 4) inject：按钮文案（问题 1）
# ------------------------------------------------------------------ #
from desktop.inject import LoginButtonInjector, _fmt_balance, _menu_model
label, title = LoginButtonInjector._label_for({
    "logged_in": True,
    "account": {"phone": "15512348602", "models": ["a", "b"]},
})
check("按钮显示脱敏用户名", label == "155****8602", f"{label!r} / {title!r}")
label2, _ = LoginButtonInjector._label_for({"logged_in": True, "account": {"models": []}})
check("无手机号时回退「已登录」", label2 == "已登录", repr(label2))
label3, _ = LoginButtonInjector._label_for({"logged_in": False, "configured": False})
check("未登录仍是「登录」", label3 == "登录", repr(label3))

# ------------------------------------------------------------------ #
# 5) inject：菜单（2026-09-16 二轮反馈）
# ------------------------------------------------------------------ #
check("余额格式化：平台成品字符串", _fmt_balance("¥16.769472 额度") == "16.77",
      repr(_fmt_balance("¥16.769472 额度")))
check("余额格式化：纯数值", _fmt_balance(16.769472) == "16.77",
      repr(_fmt_balance(16.769472)))
check("余额格式化：千分位", _fmt_balance(1234567.891) == "1,234,567.89",
      repr(_fmt_balance(1234567.891)))
check("余额格式化：空/无数字", _fmt_balance("") == "" and _fmt_balance("额度") == "",
      f"{_fmt_balance('')!r} {_fmt_balance('额度')!r}")

m_logged = _menu_model({"logged_in": True, "configured": True,
                        "account": {"phone": "15512348602",
                                    "balance": "¥16.769472 额度",
                                    "models": ["a"] * 8}})
acts = [it.get("action") for it in m_logged["items"] if isinstance(it, dict)]
check("已登录菜单无「复制 API 地址」", "copy" not in acts, str(acts))
check("余额副标题干净", m_logged["header"]["sub"] == "余额 ¥16.77 · 8 个模型",
      repr(m_logged["header"]["sub"]))

m_cfg = _menu_model({"logged_in": False, "configured": True, "account": {}})
acts_cfg = [it.get("action") for it in m_cfg["items"] if isinstance(it, dict)]
check("已配置令牌菜单也无「复制 API 地址」", "copy" not in acts_cfg, str(acts_cfg))

m_out = _menu_model({"logged_in": False, "configured": False, "account": {"balance": None}})
check("无余额时不显示余额段", "余额" not in m_out["header"]["sub"],
      repr(m_out["header"]["sub"]))

# ------------------------------------------------------------------ #
# 6) inject + manager：用户名显示（2026-09-16 三轮反馈）
# ------------------------------------------------------------------ #
from desktop.inject import _display_name
check("显示用户名 admin", _display_name({"username": "admin", "phone": "15512348602"}) == "admin",
      repr(_display_name({"username": "admin", "phone": "15512348602"})))
check("用户名是未脱敏手机号时强制打码",
      _display_name({"username": "15512348602"}) == "155****8602",
      repr(_display_name({"username": "15512348602"})))
check("无用户名回退脱敏手机号", _display_name({"phone": "15512348602"}) == "155****8602",
      repr(_display_name({"phone": "15512348602"})))
check("全空回退「已登录」", _display_name({}) == "已登录", repr(_display_name({})))

label4, _ = LoginButtonInjector._label_for({
    "logged_in": True,
    "account": {"username": "admin", "phone": "15512348602", "models": []},
})
check("按钮显示用户名而非手机号", label4 == "admin", repr(label4))

m_name = _menu_model({"logged_in": True, "configured": False,
                      "account": {"username": "admin", "phone": "15512348602",
                                  "balance": "¥16.673331 额度", "models": ["a"]}})
check("菜单标题显示用户名", m_name["header"]["title"] == "admin",
      repr(m_name["header"]["title"]))

# manager.status() 透出 username（raw 里也接得住）
with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "appdata"
    home = Path(td) / "workspace"
    root.mkdir()
    mgr2 = AuthManager(root=root, home=home)
    mgr2._store.save({"token": "sk-x",
                      "account": {"phone": "15512348602",
                                  "models": ["m1"],
                                  "raw": {"username": "admin", "sub": "u_1"}}})
    st2 = mgr2.status()
    check("status() 透出 username（从 raw 提取）",
          st2["account"].get("username") == "admin", str(st2["account"]))

print()
if failures:
    print("FAILED:", failures)
    sys.exit(1)
print("ALL CHECKS PASSED")
