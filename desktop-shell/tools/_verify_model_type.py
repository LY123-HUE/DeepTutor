# -*- coding: utf-8 -*-
"""验证 userinfo model_type 分流的快速自检（不依赖网络，用临时目录模拟真实数据流）。

覆盖：
  1) split_models_by_service：model_type 权威 > 名称启发式兜底；
  2) 4=重排序确定性丢弃（含名字无线索的模型）；
  3) 0/缺失/非法值回退名称启发式（旧平台行为不变）;
  4) AuthManager._derive 对 model_types 的提取与容错；
  5) ensure_tokengine_catalog 全链路：各服务 profile 按类型落位。
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from desktop.auth import config as cfg
from desktop.auth.catalog import ensure_tokengine_catalog, split_models_by_service
from desktop.auth.manager import AuthManager

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail else ""))
    if not cond:
        failures.append(name)


MODELS = [
    "deepseek-ai/DeepSeek-V4-Flash-0731",   # 1 文生文
    "glm-4v-image-understand",              # 1 文生文（名字带 image，启发式会错归）
    "my-custom-vector-model",               # 5 向量（名字无线索，只有类型能救）
    "secret-rerank-model-x",                # 4 重排序（名字无线索，只有类型能丢弃）
    "seedream-4.0",                         # 2 文生图
    "seedance-pro",                         # 3 文生视频
    "BAAI/bge-m3",                          # 5 向量
]
MODEL_TYPES = {
    "deepseek-ai/DeepSeek-V4-Flash-0731": 1,
    "glm-4v-image-understand": 1,
    "my-custom-vector-model": 5,
    "secret-rerank-model-x": 4,
    "seedream-4.0": 2,
    "seedance-pro": 3,
    "BAAI/bge-m3": 5,
}

# ------------------------------------------------------------------ #
# 1) model_type 权威分流
# ------------------------------------------------------------------ #
def sn(entries):
    """split 条目的模型名列表（新契约为 [{"name","model_type"}]）。"""
    return [e["name"] for e in entries]


def st(entries):
    """split 条目的 名称->model_type 映射。"""
    return {e["name"]: e.get("model_type") for e in entries}


split = split_models_by_service(MODELS, MODEL_TYPES)
check("文生文(1) -> llm", sn(split["llm"]) == ["deepseek-ai/DeepSeek-V4-Flash-0731",
      "glm-4v-image-understand"], str(split["llm"]))
check("llm 同步复制进 task（含类型）", split["task"] == split["llm"], str(split["task"]))
check("llm 每条带 model_type=1",
      all(mt == 1 for mt in st(split["llm"]).values()), str(st(split["llm"])))
check("名字带 image 但标注 1 不被启发式错归 imagegen",
      "glm-4v-image-understand" not in sn(split["imagegen"]), str(split["imagegen"]))
check("向量(5) -> embedding（含无线索名）",
      sn(split["embedding"]) == ["my-custom-vector-model", "BAAI/bge-m3"],
      str(split["embedding"]))
check("embedding 每条带 model_type=5",
      all(mt == 5 for mt in st(split["embedding"]).values()), str(st(split["embedding"])))
check("文生图(2) -> imagegen", sn(split["imagegen"]) == ["seedream-4.0"], str(split["imagegen"]))
check("imagegen 带 model_type=2", st(split["imagegen"]).get("seedream-4.0") == 2, "")
check("文生视频(3) -> videogen", sn(split["videogen"]) == ["seedance-pro"], str(split["videogen"]))
check("videogen 带 model_type=3", st(split["videogen"]).get("seedance-pro") == 3, "")
all_names = [n for entries in split.values() for n in sn(entries)]
check("重排序(4) 确定性丢弃（名字无线索也丢）",
      "secret-rerank-model-x" not in all_names, str(split))

# ------------------------------------------------------------------ #
# 2) 未标注(0)/缺失/非法 -> 保守归对话（无名称兜底）
# ------------------------------------------------------------------ #
split0 = split_models_by_service(
    ["BAAI/bge-m3", "seedance-pro", "totally-unknown-chat"],
    {"BAAI/bge-m3": 0, "seedance-pro": 99, "totally-unknown-chat": None},
)
check("0=未标注归对话", "BAAI/bge-m3" in sn(split0["llm"]), str(split0["llm"]))
check("未知值(99)归对话", "seedance-pro" in sn(split0["llm"]), str(split0["llm"]))
check("None 值归对话", "totally-unknown-chat" in sn(split0["llm"]), str(split0["llm"]))
check("未知类型落 model_type=1",
      st(split0["llm"]).get("totally-unknown-chat") == 1, "")

# ------------------------------------------------------------------ #
# 3) 不传 model_types：所有模型归对话（无名称分流）
# ------------------------------------------------------------------ #
legacy = split_models_by_service(["BAAI/bge-m3", "seedance-pro", "gpt-4o"])
check("无类型时全部归对话",
      sn(legacy["llm"]) == ["BAAI/bge-m3", "seedance-pro", "gpt-4o"], str(legacy))
check("无类型时 embedding/imagegen/videogen 为空",
      legacy["embedding"] == [] and legacy["imagegen"] == [] and legacy["videogen"] == [],
      str(legacy))
check("无类型对话带 model_type=1",
      all(mt == 1 for mt in st(legacy["llm"]).values()), str(st(legacy["llm"])))

# ------------------------------------------------------------------ #
# 3.5) 平台全标 1（旧事故）：纯 model_type 分流下全部归对话
#      （平台已修复 model_type，此场景仅作回归保护）
# ------------------------------------------------------------------ #
bad_platform = {
    "MiniMax/MiniMax-M2.7": 1,
    "Qwen/Qwen3-VL-8B-Instruct": 1,
    "Qwen/Qwen3-Reranker-8B": 1,
    "Qwen/Qwen3-Embedding-8B": 1,
}
split_bad = split_models_by_service(list(bad_platform), bad_platform)
sn_bad = {s: sn(e) for s, e in split_bad.items()}
check("全标1：全部归对话（含原 Reranker/Embedding）",
      sn_bad["llm"] == list(bad_platform), str(sn_bad["llm"]))
check("全标1：embedding/imagegen/videogen 为空",
      split_bad["embedding"] == [] and split_bad["imagegen"] == []
      and split_bad["videogen"] == [], str(sn_bad))

# ------------------------------------------------------------------ #
# 4) _derive：严格按契约解析（models = [{"model_name","model_type"}]）
# ------------------------------------------------------------------ #
with tempfile.TemporaryDirectory() as td:
    mgr = AuthManager(root=Path(td) / "root", home=Path(td) / "home")
    structured = [{"model_name": n, "model_type": t} for n, t in MODEL_TYPES.items()]
    models, mts, _, _, _ = mgr._derive({"models": structured, "phone": "15512348602"})
    check("_derive 解析契约结构体数组", models == MODELS and mts == MODEL_TYPES,
          f"{models} / {mts}")
    check("空数组 = 平台明确无授权模型（不用本地兜底）",
          mgr._derive({"models": []})[0] == [], "")
    check("缺 models 字段才用 DEFAULT_MODELS 兜底",
          mgr._derive({})[0] == list(cfg.DEFAULT_MODELS), "")
    models, mts, _, _, _ = mgr._derive(
        {"models": [{"model_name": "x", "model_type": 5.0},
                    {"model_type": 1},
                    {"model_name": ""},
                    "not-a-dict"]})
    check("缺名/非 dict 项跳过，类型归一 int",
          models == ["x"] and mts == {"x": 5}, f"{models} / {mts}")

# ------------------------------------------------------------------ #
# 4.5) 业务令牌：严格按契约取 userinfo.ai_token（无 token 响应回退）
# ------------------------------------------------------------------ #
pick_token = lambda account: str(account.get(cfg.USERINFO_AI_TOKEN_FIELD) or "")
check("契约：ai_token 从 userinfo 下发", pick_token({"ai_token": "sk-new"}) == "sk-new")
check("缺失 ai_token -> 登录报 no_token", pick_token({}) == "")

# ------------------------------------------------------------------ #
# 5) ensure_tokengine_catalog 全链路：各服务 profile 按类型落位
# ------------------------------------------------------------------ #
with tempfile.TemporaryDirectory() as td:
    home = Path(td) / "workspace"
    ensure_tokengine_catalog(
        home=home, api_key="sk-test-token",
        base_url="https://tokengine.hanyoai.com/v1",
        models=MODELS, model_types=MODEL_TYPES,
    )
    cat = json.loads((home / "data/user/settings/model_catalog.json").read_text(
        encoding="utf-8"))
    names = lambda svc: [m["model"] for p in cat["services"][svc]["profiles"]
                         if p.get("connection_id") == "tokengine"
                         for m in (p.get("models") or [])]
    types_of = lambda svc: [m.get("model_type") for p in cat["services"][svc]["profiles"]
                            if p.get("connection_id") == "tokengine"
                            for m in (p.get("models") or [])]
    check("catalog llm 按类型落位", names("llm") == [
        "deepseek-ai/DeepSeek-V4-Flash-0731", "glm-4v-image-understand"], str(names("llm")))
    check("catalog llm 落库带 model_type=1", types_of("llm") == [1, 1], str(types_of("llm")))
    check("catalog embedding 落位", names("embedding") == [
        "my-custom-vector-model", "BAAI/bge-m3"], str(names("embedding")))
    check("catalog embedding 落库带 model_type=5",
          types_of("embedding") == [5, 5], str(types_of("embedding")))
    check("catalog imagegen 落位", names("imagegen") == ["seedream-4.0"], str(names("imagegen")))
    check("catalog videogen 落位", names("videogen") == ["seedance-pro"], str(names("videogen")))
    check("catalog task 与 llm 一致", names("task") == names("llm"), str(names("task")))
    check("catalog task 带 model_type=1",
          all(t == 1 for t in types_of("task")), str(types_of("task")))
    check("重排序模型未写入任何服务",
          "secret-rerank-model-x" not in sum((names(s) for s in
                                              ("llm", "task", "embedding", "imagegen", "videogen")), []),
          "")

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    sys.exit(1)
print("ALL PASS")
