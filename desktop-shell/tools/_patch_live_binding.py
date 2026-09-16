# -*- coding: utf-8 -*-
"""一次性：把本机已安装 EduBuddy 的绑定数据从生产对齐到 endpoints.json 指向的测试环境。

- 备份先行（.bak-<date>）
- model_catalog.json：tokengine connection + 活动 profile 的 base_url
- auth.json（DPAPI）：relay_base / relay_source（菜单「复制 API 地址」「关于」读这里）
"""
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from desktop.auth.store import TokenStore

APPDATA_ROOT = Path.home() / "AppData" / "Local" / "EduBuddy"
WORKSPACE = Path.home() / "EduBuddy"
CATALOG = WORKSPACE / "data" / "user" / "settings" / "model_catalog.json"
STAMP = time.strftime("%Y%m%d-%H%M%S")

OLD = "https://tokengine.hanyoai.com/v1"
NEW = "https://tokengine-t.hanyoai.com/v1"

changed = []

# ---- 1) model_catalog.json ------------------------------------------------ #
bak = CATALOG.with_suffix(f".json.bak-{STAMP}")
shutil.copy2(CATALOG, bak)
print(f"backup: {bak}")
cat = json.loads(CATALOG.read_text(encoding="utf-8"))
hits = 0
for conn in cat.get("connections", []):
    if isinstance(conn, dict) and conn.get("id") == "tokengine" \
            and str(conn.get("base_url", "")).rstrip("/") == OLD:
        conn["base_url"] = NEW
        hits += 1
llm = (cat.get("services") or {}).get("llm") or {}
for prof in llm.get("profiles") or []:
    if isinstance(prof, dict) and prof.get("connection_id") == "tokengine" \
            and str(prof.get("base_url", "")).rstrip("/") == OLD:
        prof["base_url"] = NEW
        hits += 1
if hits:
    CATALOG.write_text(json.dumps(cat, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
    changed.append(f"model_catalog.json: {hits} 处 base_url -> {NEW}")
else:
    print("model_catalog.json: 无需改动")

# ---- 2) auth.json（DPAPI 解密 → 改 → 原格式回写）--------------------------- #
store = TokenStore(APPDATA_ROOT)
payload = store.load()
if payload:
    abak = APPDATA_ROOT / (TokenStore.FILE_NAME + f".bak-{STAMP}")
    shutil.copy2(APPDATA_ROOT / TokenStore.FILE_NAME, abak)
    print(f"backup: {abak}")
    old_relay = payload.get("relay_base")
    if str(old_relay or "").rstrip("/") != NEW:
        payload["relay_base"] = NEW
        payload["relay_source"] = "local-override"
        store.save(payload)
        changed.append(f"auth.json: relay_base {old_relay} -> {NEW}")
    else:
        print("auth.json: relay 已一致")
else:
    print("auth.json: 无数据（未登录？），跳过")

print()
for line in changed:
    print("CHANGED:", line)
if not changed:
    print("NOTHING CHANGED")
