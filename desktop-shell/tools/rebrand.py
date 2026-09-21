"""打包期品牌重写：staging 里的 DeepTutor → EduBuddy（只动构建产物，不动源码）。

为什么需要这个脚本
------------------
DeepTutor 是开源上游项目名（Python 包名、import、类名、仓库地址都是它），
EduBuddy 是桌面发行版面向最终用户的品牌。改源码会产生上百个文件的 diff、
持续制造合并冲突；因此品牌重写放在打包阶段：build_runtime 把源码装进 staging
之后、打 runtime.zip / ISCC 之前，对 staging 产物做一次文本重写。

前端（deeptutor_web）：直接在 Next.js 构建产出的压缩 JS/HTML 上文本替换。
minify 不会拆开字符串字面量，"DeepTutor" 在 chunk 里永远连续完整，直接
替换字面量内容不会破坏 JS 语法。

后端 / CLI（deeptutor、deeptutor_cli）：只在 .py 的【字符串字面量】与
.yaml prompt 里替换——AI 身份、用户可见报错/状态文案在这里；类名、import、
注解、注释天然不受影响。

替换规则（安全边界）
--------------------
1. 只替换驼峰品牌名 ``DeepTutor`` → ``EduBuddy``；
     - ``deeptutor`` 小写（Python 包名 / import / 路径 / pip 包名 /
       indexedDB 库名 / docs.deeptutor.info 域名）不碰；
     - ``DEEPTUTOR_*`` 大写（环境变量）不碰。
2. 品牌词必须带【标识符边界】：前后紧贴字母/数字/下划线时不替换。
   ``DeepTutorApp`` / ``DeepTutorParser`` / ``DeepTutorError`` 因此全部
   豁免，包括它们出现在字符串内部时（如 ``__all__``、
   ``"module:DeepTutorParser"`` 反射路径）。
3. Python 文件用标准库 ``tokenize`` 精确定位字符串 token，绝不靠正则猜
   引号配对——避免撇号/嵌套引号跨行错位把真实代码吞进"伪字符串"。
4. 任何 URL 中的品牌名受保护（如 github.com/HKUDS/DeepTutor），换了会 404。
5. bytes 字面量（b"..."）不替换（长度变化对二进制协议有风险）。
6. 幂等：产物已是 EduBuddy 时再次运行替换 0 处，无副作用。

Usage:
    python tools/rebrand.py                 # 重写默认 staging
    python tools/rebrand.py --dry-run       # 只统计不写入
    python tools/rebrand.py --root <path>   # 指定 site-packages 根目录
"""
from __future__ import annotations

import argparse
import ast
import io
import re
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SITE_PACKAGES = ROOT / "runtime-build" / "staging" / "python" / "Lib" / "site-packages"

# 只处理本项目的三个发行包；其余第三方包一律不扫
TARGET_PACKAGES = ("deeptutor", "deeptutor_cli", "deeptutor_web")

# 全文替换的文本类型；.py 走 tokenize 专用通道；其余（.pyc/.node/.png 等）跳过
PLAINTEXT_SUFFIXES = {
    ".yaml", ".yml", ".json", ".js", ".mjs", ".cjs",
    ".html", ".htm", ".css", ".txt",
}
SKIP_SUFFIXES = {".pyc", ".pyo", ".map"}

BRAND = "DeepTutor"
NEW_BRAND = "EduBuddy"

# 含品牌名的 URL —— 仓库/官网等真实地址，必须原样保留。
# 同时覆盖 JS 编译产物里的转义形式（https:\/\/... 斜杠前带反斜杠）。
URL_RE = re.compile(
    r"[a-zA-Z][a-zA-Z0-9+.-]*:(?:\\?/){2}[^\s\"'<>）)]*" + BRAND + r"[^\s\"'<>）)]*")
# 无 scheme 的仓库路径（如 API 前缀 "/HKUDS/DeepTutor/releases/tag/"），
# 同样覆盖转义形式 HKUDS\/DeepTutor。
REPO_PATH_RE = re.compile(
    r"HKUDS(?:\\?/)+" + BRAND + r"(?:(?:\\?/)[^\s\"'<>）)]*)?")

# 品牌词必须是“独立词”：前后不能紧贴标识符字符。
# DeepTutorApp / DeepTutorParser / xxxDeepTutor 一律不换；
# “你是 DeepTutor，”/“DeepTutor.”/“DeepTutor's” 正常替换。
BRAND_BOUNDED_RE = re.compile(r"(?<![A-Za-z0-9_])" + BRAND + r"(?![A-Za-z0-9_])")

# 自检用：EduBuddy 与标识符字符相邻，说明有技术标识符被误伤
BAD_IDENT_RE = re.compile(r"[A-Za-z0-9_]" + NEW_BRAND + r"|" + NEW_BRAND + r"[A-Za-z0-9_]")

# Python 3.12 起 f-string 被词法拆成 FSTRING_START/MIDDLE/END
_HAS_FSTRING_MIDDLE = hasattr(tokenize, "FSTRING_MIDDLE")
# 旧版本 f-string 是单个 STRING token，body 里的 {表达式} 需要暂存保护
_FEXPR_OLD_RE = re.compile(r"\{[^{}]*\}")
# STRING token 的字符串前缀（r/u/b/f，含各种大小写组合）
_PY_PREFIX_RE = re.compile(r"(?i)^[rubf]{0,3}")


def _stash_urls(text: str, stash: list[str]) -> str:
    """把含品牌名的 URL / 仓库路径换成占位符，替换完品牌后再还原。"""
    def _sub(m: re.Match) -> str:
        stash.append(m.group(0))
        return f"\x00URL{len(stash) - 1}\x00"
    text = URL_RE.sub(_sub, text)
    text = REPO_PATH_RE.sub(_sub, text)
    return text


def _restore_urls(text: str, stash: list[str]) -> str:
    for i, url in enumerate(stash):
        text = text.replace(f"\x00URL{i}\x00", url)
    return text


def _brand_bounded(text: str) -> tuple[str, int]:
    """带标识符边界的品牌替换。返回 (新文本, 替换次数)。"""
    hits = len(BRAND_BOUNDED_RE.findall(text))
    if not hits:
        return text, 0
    return BRAND_BOUNDED_RE.sub(NEW_BRAND, text), hits


def rebrand_plain(text: str) -> tuple[str, int]:
    """全文替换（yaml/json/js/html 等）：URL 暂存 + 标识符边界。"""
    stash: list[str] = []
    protected = _stash_urls(text, stash)
    out, count = _brand_bounded(protected)
    return _restore_urls(out, stash), count


def _rebrand_string_body(body: str, is_fstring_legacy: bool) -> tuple[str, int]:
    """替换一个 Python 字符串【字面量内容】里的品牌名。"""
    # 旧版 Python（<3.12）的 f-string：先把 {表达式} 暂存，表达式不替换
    fexpr_stash: list[str] = []
    work = body
    if is_fstring_legacy:
        work = _FEXPR_OLD_RE.sub(
            lambda m: (fexpr_stash.append(m.group(0)) or f"\x01F{len(fexpr_stash) - 1}\x01"),
            work,
        )
    url_stash: list[str] = []
    work = _stash_urls(work, url_stash)
    work, hits = _brand_bounded(work)
    work = _restore_urls(work, url_stash)
    if is_fstring_legacy:
        for i, expr in enumerate(fexpr_stash):
            work = work.replace(f"\x01F{i}\x01", expr)
    return work, hits


def _rebrand_string_token(token_text: str) -> tuple[str, int]:
    """拆 STRING token 的 前缀/引号/内容/引号，只改内容。"""
    m = _PY_PREFIX_RE.match(token_text)
    assert m is not None
    prefix = m.group(0)
    rest = token_text[len(prefix):]
    # bytes 字面量不替换（避免二进制数据长度/语义变化）
    if "b" in prefix.lower():
        return token_text, 0
    if rest.startswith(('"""', "'''")):
        quote = rest[:3]
    else:
        quote = rest[0]
    body = rest[len(quote):-len(quote)]
    # 3.12+ 的 f-string 不会以 STRING token 出现；只有旧版本需要保护 {表达式}
    is_fstring_legacy = ("f" in prefix.lower()) and not _HAS_FSTRING_MIDDLE
    new_body, hits = _rebrand_string_body(body, is_fstring_legacy)
    return f"{prefix}{quote}{new_body}{quote}", hits


def rebrand_python_source(text: str) -> tuple[str, int]:
    """只替换 Python 字符串字面量内部的品牌名。

    用 tokenize 精确定位 STRING / FSTRING_MIDDLE token，再按字符偏移原位
    重写，代码、注解、注释、import 一律不动。tokenize 失败（文件本身语法
    不完整）时安全跳过，不猜测、不破坏。
    """
    # 行起始偏移表，把 token 的 (row, col) 换成绝对字符偏移
    line_starts = [0]
    for line in text.splitlines(keepends=True):
        line_starts.append(line_starts[-1] + len(line))

    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return text, 0

    edits: list[tuple[int, int, str]] = []
    total = 0
    for tok in tokens:
        new_tok: str | None = None
        hits = 0
        if tok.type == tokenize.STRING:
            new_tok, hits = _rebrand_string_token(tok.string)
        elif _HAS_FSTRING_MIDDLE and tok.type == tokenize.FSTRING_MIDDLE:
            # 3.12+：这就是 f-string 里的纯字面文本段（不含 {表达式}）
            new_tok, hits = _rebrand_string_body(tok.string, False)
        if hits and new_tok is not None and new_tok != tok.string:
            s = line_starts[tok.start[0] - 1] + tok.start[1]
            e = line_starts[tok.end[0] - 1] + tok.end[1]
            edits.append((s, e, new_tok))
            total += hits

    if not edits:
        return text, 0
    # 从后往前原位替换，偏移互不影响
    for s, e, new_tok in sorted(edits, reverse=True):
        text = text[:s] + new_tok + text[e:]
    return text, total


def rebrand_root(site_packages: Path, dry_run: bool = False) -> int:
    if not site_packages.is_dir():
        raise SystemExit(f"site-packages 不存在: {site_packages}")

    grand_total = 0
    files_changed = 0
    for pkg in TARGET_PACKAGES:
        pkg_dir = site_packages / pkg
        if not pkg_dir.is_dir():
            print(f"  跳过（不存在）: {pkg}")
            continue
        for f in pkg_dir.rglob("*"):
            if not f.is_file() or f.suffix in SKIP_SUFFIXES:
                continue
            if f.suffix == ".py":
                transform = rebrand_python_source
            elif f.suffix in PLAINTEXT_SUFFIXES:
                transform = rebrand_plain
            else:
                continue
            try:
                original = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, ValueError):
                continue  # 伪装成文本后缀的二进制，跳过
            new_text, hits = transform(original)
            if hits:
                grand_total += hits
                files_changed += 1
                if dry_run:
                    print(f"  [dry-run] {hits:3d} 处  {f.relative_to(site_packages)}")
                else:
                    f.write_text(new_text, encoding="utf-8", newline="")
                    print(f"  {hits:3d} 处  {f.relative_to(site_packages)}")

    mode = "（dry-run，未写入）" if dry_run else ""
    print(f"\n重写完成{mode}: {files_changed} 个文件，共 {grand_total} 处 {BRAND} → {NEW_BRAND}")
    return grand_total


# ---- 安全自检 -------------------------------------------------------------

# 必须保持原名的关键技术标识符（出现位置 → 必须包含的子串）
_IDENT_INVARIANTS = (
    ("deeptutor/app/facade.py", "class DeepTutorApp"),
    ("deeptutor/core/errors.py", "DeepTutorError"),
    ("deeptutor/app/__init__.py", '"DeepTutorApp"'),
    ("deeptutor/services/rag/pipelines/lightrag/parser.py", "DeepTutorParser"),
    ("deeptutor/services/rag/pipelines/lightrag/engine.py", ":DeepTutorParser"),
    ("deeptutor_cli/common.py", "from deeptutor.app import DeepTutorApp"),
    ("deeptutor_cli/common.py", "app: DeepTutorApp"),
)


def self_check(site_packages: Path) -> None:
    """关键安全自检：技术标识符没被动、URL 没动、.py 仍可解析、品牌进了 prompt。"""
    print("\n=== 安全自检 ===")
    checks: list[tuple[bool, str]] = []

    # 1) 关键技术标识符完好
    for rel, needle in _IDENT_INVARIANTS:
        p = site_packages / rel
        if p.exists():
            checks.append((needle in p.read_text(encoding="utf-8"),
                           f"{rel} 保留 {needle!r}"))

    # 2) 全包扫描：EduBuddy 不得与标识符字符相邻（类名/反射路径误伤检测）
    bad_ident: list[str] = []
    # 3) URL 保护检测
    bad_urls = 0
    scan_suffixes = {".py", ".js", ".mjs", ".cjs", ".json",
                     ".yaml", ".yml", ".html", ".htm", ".css", ".txt"}
    py_syntax_errors: list[str] = []
    for pkg in TARGET_PACKAGES:
        pkg_dir = site_packages / pkg
        if not pkg_dir.is_dir():
            continue
        for f in pkg_dir.rglob("*"):
            if not f.is_file() or f.suffix not in scan_suffixes:
                continue
            try:
                src = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, ValueError):
                continue
            m = BAD_IDENT_RE.search(src)
            if m:
                bad_ident.append(f"{f.relative_to(site_packages)}: …{m.group(0)}…")
            if "HKUDS/EduBuddy" in src or "HKUDS\\/EduBuddy" in src:
                bad_urls += 1
            if f.suffix == ".py":
                try:
                    ast.parse(src, filename=str(f))
                except SyntaxError as e:
                    py_syntax_errors.append(f"{f.relative_to(site_packages)}: {e}")

    checks.append((len(bad_ident) == 0,
                   f"无标识符相邻误伤（EduBuddy 紧贴字母/下划线：{len(bad_ident)} 处，应为 0）"))
    for sample in bad_ident[:5]:
        print(f"    ❌ 误伤样本: {sample}")
    checks.append((bad_urls == 0,
                   f"仓库 URL 未被误改（HKUDS/EduBuddy 出现 {bad_urls} 处，应为 0）"))
    checks.append((len(py_syntax_errors) == 0,
                   f"全部 .py 语法可解析（失败 {len(py_syntax_errors)} 个）"))
    for sample in py_syntax_errors[:5]:
        print(f"    ❌ 语法错误: {sample}")

    # 4) 品牌确实进入 prompt（AI 身份）与前端产物
    zh_chat = site_packages / "deeptutor" / "agents" / "chat" / "prompts" / "zh" / "chat_agent.yaml"
    if zh_chat.exists():
        checks.append(("EduBuddy" in zh_chat.read_text(encoding="utf-8"),
                       "中文 chat 身份 prompt 已含 EduBuddy"))

    ok = True
    for passed, msg in checks:
        print(f"  {'✅' if passed else '❌'} {msg}")
        ok = ok and passed
    if not ok:
        raise SystemExit("自检失败：品牌重写可能误伤了技术标识符或破坏了语法，请检查！")
    print("自检全部通过 ✅")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=DEFAULT_SITE_PACKAGES,
                    help=f"site-packages 根目录（默认: {DEFAULT_SITE_PACKAGES}）")
    ap.add_argument("--dry-run", action="store_true", help="只统计替换点，不写入文件")
    ap.add_argument("--skip-check", action="store_true", help="跳过安全自检")
    args = ap.parse_args()

    print(f"品牌重写目标: {args.root}")
    print(f"  {BRAND} → {NEW_BRAND}（仅驼峰展示名；包名/环境变量/标识符/URL 受保护）\n")
    rebrand_root(args.root, dry_run=args.dry_run)
    if not args.dry_run and not args.skip_check:
        self_check(args.root)


if __name__ == "__main__":
    main()
