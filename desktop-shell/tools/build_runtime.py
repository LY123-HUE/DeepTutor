"""Build the offline runtime bundle shipped by the click-installer.

Produces  dist/runtime.zip  containing two relocatable components:

    runtime/
      python/   # python.org 3.12 embeddable distribution with `deeptutor`
                # installed flat into Lib/site-packages (cp312 wheels)
      node/     # portable Node.js 22 LTS (node.exe + npm)

The shell extracts this zip into %LOCALAPPDATA%\\EduBuddy\\runtime on
first launch, then runs deeptutor via:
    python/python.exe -c "from deeptutor_cli.main import main; ..."

Both components are relocatable by design (no absolute paths / no venv links),
so end users get a fully offline click-to-install experience.

Usage:
    python tools/build_runtime.py            # download + build
    python tools/build_runtime.py --clean    # fresh build
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / "runtime-build"
CACHE = STAGE / "cache"
STAGING_PY = STAGE / "staging" / "python"
STAGING_NODE = STAGE / "staging" / "node"
DIST = ROOT / "dist"

# Relocatable Windows components (edit to bump versions)
PY_VER = "3.12.7"
PY_URL = f"https://www.python.org/ftp/python/{PY_VER}/python-{PY_VER}-embed-amd64.zip"
PY_ZIP = CACHE / f"python-{PY_VER}-embed-amd64.zip"

NODE_VER = "v22.22.2"
NODE_URL = f"https://nodejs.org/dist/{NODE_VER}/node-{NODE_VER}-win-x64.zip"
NODE_ZIP = CACHE / f"node-{NODE_VER}-win-x64.zip"

# A system cp312 interpreter used only to resolve cp312 wheels into the
# embeddable distribution during the *build* (not needed at run time).
def _find_build_py() -> Path | None:
    env = os.environ.get("BUILD_PY")
    if env:
        return Path(env)
    candidates = list(Path.home().glob(
        "AppData/Roaming/uv/python/cpython-3.1[12]-*/python.exe"))
    candidates += list(Path.home().glob(
        ".local/share/uv/python/cpython-3.1[12]-*/python.exe"))
    for c in candidates:
        if c.exists():
            return c
    return None


BUILD_PY = _find_build_py()


def log(msg: str) -> None:
    print(msg, flush=True)


def download(url: str, dest: Path) -> None:
    if dest.exists() and dest.stat().st_size > 1_000_000:
        log(f"cached: {dest.name}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    log(f"downloading {url} ...")
    urllib.request.urlretrieve(url, dest)
    log(f"downloaded {dest.stat().st_size / 1e6:.1f} MB")


def extract(zip_path: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(target)
    # strip a possible single wrapper dir (node zip has one)
    subs = [p for p in target.iterdir() if p.is_dir()]
    if len(subs) == 1 and not list(target.glob("*.exe")) and not list(target.glob("python*.dll")):
        inner = subs[0]
        for item in list(inner.iterdir()):
            shutil.move(str(item), str(target / item.name))
        inner.rmdir()
    log(f"extracted {zip_path.name} -> {target}")


def enable_site(path: Path) -> None:
    """Uncomment `import site` and ensure Lib\\site-packages is on sys.path."""
    pth = next(path.glob("python*._pth"))
    lines = [l.rstrip() for l in pth.read_text(encoding="utf-8").splitlines()]
    out: list[str] = []
    has_sp, has_site = False, False
    for line in lines:
        if line.strip() == "#import site":
            out.append("import site")
            has_site = True
        else:
            out.append(line)
        if "site-packages" in line:
            has_sp = True
    if not has_sp:
        out.append("Lib\\site-packages")
    if not has_site:
        out.append("import site")
    pth.write_text("\n".join(out) + "\n", encoding="utf-8")
    log(f"enabled site-packages in {pth.name}")


def install_deeptutor(target: Path) -> None:
    """pip install --target deeptutor into the embeddable's site-packages."""
    if BUILD_PY is None or not BUILD_PY.exists():
        raise SystemExit("no cp312 build interpreter found (set BUILD_PY=...py)")
    dest = target / "Lib" / "site-packages"
    dest.mkdir(parents=True, exist_ok=True)
    log(f"pip installing deeptutor with {BUILD_PY} ...")
    res = subprocess.run(
        [str(BUILD_PY), "-m", "pip", "install", "--upgrade", "--no-compile", "--target", str(dest), "deeptutor"],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        log(res.stdout[-3000:])
        log(res.stderr[-3000:])
        raise SystemExit(f"pip install deeptutor failed (rc={res.returncode})")
    log("deeptutor installed into embeddable runtime")


def smoke_test(py_exe) -> None:
    import importlib.util

    code = (
        "import deeptutor_cli, deeptutor, deeptutor_web; "
        "print('runtime imports OK:', deeptutor_cli.__file__)"
    )
    res = subprocess.run([str(py_exe), "-c", code], capture_output=True, text=True, timeout=120)
    log("import check: " + (res.stdout.strip() or res.stderr.strip()))


# Runtime packages that are safe to drop from a packaged install:
#  - litellm           : deeptutor dropped it in v1.0.0-beta.3 (native SDKs); only
#                        pageindex imports it lazily for niche local-chat features
#  - boto3/botocore/...: AWS; only llama_index.core.utilities.aws_utils (lazy) uses it
#  - hf_xet            : optional huggingface_hub download accelerator
#  - bin/              : pip console-script launchers (app runs via run_deeptutor.py)
#  - PyWin32.chm       : pywin32 help file
def PRUNE_GLOBS() -> list[str]:
    return [
        "litellm", "litellm-*.dist-info",
        "boto3", "boto3-*.dist-info", "botocore", "botocore-*.dist-info",
        "s3transfer", "s3transfer-*.dist-info",
        "hf_xet", "hf_xet-*.dist-info",
        "bin", "PyWin32.chm",
    ]


def prune_runtime(site_packages: Path) -> None:
    """Remove unneeded heavy packages; log how much room this frees."""
    import glob

    removed_mb = 0.0
    for pat in PRUNE_GLOBS():
        for entry in glob.glob(str(site_packages / pat)):
            p = Path(entry)
            mb = sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1e6 if p.is_dir() \
                else p.stat().st_size / 1e6
            removed_mb += mb
            shutil.rmtree(entry, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
            log(f"pruned: {p.name} ({mb:.1f} MB)")
    log(f"prune done, freed ~{removed_mb:.1f} MB")


def _prepared(py: Path, node: Path) -> bool:
    """True if staging already holds an extractable python + node."""

    def check(root: Path, marker: str) -> bool:
        if not (root / marker).exists():
            return False
        return any(root.glob("python*.dll")) if root is py else True

    return check(py, "python.exe") and check(node, "node.exe")


def build(make_zip: bool = False) -> None:
    DIST.mkdir(parents=True, exist_ok=True)

    if _prepared(STAGING_PY, STAGING_NODE):
        log("staging under " + str(STAGE) + " already prepared; reusing.")
    else:
        download(PY_URL, PY_ZIP)
        download(NODE_URL, NODE_ZIP)
        extract(PY_ZIP, STAGING_PY)
        extract(NODE_ZIP, STAGING_NODE)
        enable_site(STAGING_PY)

    # pip install only when deeptutor is not yet present in the embeddable
    sp = STAGING_PY / "Lib" / "site-packages" / "deeptutor"
    if not sp.exists():
        install_deeptutor(STAGING_PY)
    else:
        log("deeptutor already in embeddable site-packages; skipping pip")
    smoke_test(STAGING_PY / "python.exe")

    sp = STAGING_PY / "Lib" / "site-packages"
    if (sp / "litellm").exists() or (sp / "boto3").exists():
        prune_runtime(sp)
    else:
        log("runtime already pruned; skipping")
    smoke_test(STAGING_PY / "python.exe")  # re-verify after pruning

    # The portable flow (make_portable.py) packs `runtime-build/staging` as-is,
    # so the zip below is OPT-IN. Deflating thousands of tiny files is slow, so
    # it is skipped unless explicitly requested (e.g. for the onefile/Inno path).
    if not make_zip:
        log("staging ready at " + str(STAGE / "staging") + " (zip skipped)")
        return
    out_zip = DIST / "runtime.zip"
    out_zip.unlink(missing_ok=True)
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for root_ in (STAGING_PY, STAGING_NODE):
            base = "python" if root_ is STAGING_PY else "node"
            for f in sorted(root_.rglob("*")):
                if f.is_file():
                    zf.write(f, f"{base}/{f.relative_to(root_).as_posix()}")
    size = out_zip.stat().st_size / 1e6
    log(f"built {out_zip} ({size:.1f} MB)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true", help="rebuild staging from scratch")
    ap.add_argument("--no-zip", action="store_true",
                    help="do NOT build dist/runtime.zip (portable flow uses staging dir)")
    args = ap.parse_args()
    build(make_zip=not args.no_zip)


if __name__ == "__main__":
    main()
