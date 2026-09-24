"""On-demand provisioning of runtime-only heavy dependencies.

The slim offline runtime (see tools/build_runtime.py PRUNE_GLOBS) omits the
heavy RAG / document-parse wheels (llama_index, faiss-cpu, pymupdf, ...) to
keep EduBuddySetup.exe small. Those wheels are lazy-imported at runtime, so
startup and chat never touch them &mdash; but the first time a user opens the
knowledge-base or uploads a document, they must be present.

This module gives the desktop shell a small "feature installer": it ensures pip
is available inside the embeddable python (which ships without pip) and then
pip-installs the missing wheels into that runtime's site-packages on demand, so
the full feature set works on the slim build while the base package stays small.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

log = logging.getLogger("dt.ensure")

# pip package -> module that should import after install.
# Ordered roughly by dependency weight; core KB pipeline first.
REQUIRED: dict[str, str] = {
    "llama-index": "llama_index",
    "faiss-cpu": "faiss",
    "pymupdf": "fitz",
    "pymupdf4llm": "pymupdf4llm",
    "pdfminer.six": "pdfminer",
    "pypdf": "pypdf",
    "nltk": "nltk",
}

GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"


def runtime_python() -> Path | None:
    """Locate the embeddable python inside the managed/managed runtime."""
    import desktop.runtime as rt

    for base in (rt.RUNTIME, rt.EXE_DIR / "runtime", rt.APP_DIR / "runtime"):
        cand = base / "python" / "python.exe"
        if cand.exists():
            return cand
    return None


def missing_modules(python: Path) -> list[str]:
    """Return the list of REQUIRED pip packages whose modules are absent."""
    missing: list[str] = []
    for pip_pkg, mod in REQUIRED.items():
        code = (
            "import importlib.util,sys;"
            "ok=all(importlib.util.find_spec(m) is not None for m in "
            "%r.split(','));sys.exit(0 if ok else 1)" % mod
        )
        try:
            r = subprocess.run(
                [str(python), "-c", code],
                capture_output=True, text=True, timeout=60,
                creationflags=0x08000000 if os.name == "nt" else 0,
            )
            if r.returncode != 0:
                missing.append(pip_pkg)
        except Exception as exc:  # noqa: BLE001
            log.warning("probe %s failed: %s", pip_pkg, exc)
            missing.append(pip_pkg)
    return missing


def _ensure_pip(python: Path) -> bool:
    """Make sure `python -m pip` works inside the embeddable runtime."""
    check = subprocess.run(
        [str(python), "-m", "pip", "--version"],
        capture_output=True, text=True, timeout=60,
        creationflags=0x08000000 if os.name == "nt" else 0,
    )
    if check.returncode == 0:
        return True
    log.info("pip absent; bootstrapping via get-pip.py")
    try:
        with tempfile.TemporaryDirectory() as td:
            gp = Path(td) / "get-pip.py"
            urllib.request.urlretrieve(GET_PIP_URL, str(gp))
            log.info("downloaded %s", GET_PIP_URL)
            r = subprocess.run(
                [str(python), str(gp), "--no-warn-script-location"],
                capture_output=True, text=True, timeout=300,
                creationflags=0x08000000 if os.name == "nt" else 0,
            )
            if r.returncode != 0:
                log.error("get-pip failed: %s", r.stderr[-2000:])
                return False
        return True
    except Exception as exc:  # noqa: BLE001
        log.exception("pip bootstrap failed")
        return False


def install_missing(python: Path, packages: list[str],
                    on_line=None) -> dict:
    """pip-install the given packages into the embeddable runtime."""
    def say(msg: str) -> None:
        log.info("%s", msg)
        if on_line:
            try:
                on_line(msg)
            except Exception:  # noqa: BLE001
                pass

    if not _ensure_pip(python):
        return {"ok": False, "error": "无法初始化 pip（请检查网络后重试）"}
    say("正在联网安装知识库/文档功能组件（首次使用，约需 1~3 分钟）…")
    cmd = [str(python), "-m", "pip", "install", "--no-input",
           "--disable-pip-version-check", *packages]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=1800,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        if r.returncode != 0:
            say("功能组件安装失败")
            log.error("pip install failed: %s", r.stderr[-3000:])
            return {"ok": False, "error": "功能组件安装失败，请检查网络后重试"}
        say("功能组件安装完成 ✓")
        return {"ok": True, "installed": packages}
    except Exception as exc:  # noqa: BLE001
        log.exception("install failed")
        return {"ok": False, "error": f"安装失败：{exc}"}


# -- high-level entry used by Api.ensure_rag() ------------------------------- #
def ensure_rag(on_line=None) -> dict:
    """Idempotently provision the RAG/document wheels for the current runtime.

    Returns:
        {ok, state, installed?, error?}
        state: "complete" (nothing needed) | "installed" | "failed"
    """
    python = runtime_python()
    if python is None:
        return {"ok": False, "error": "未找到内置运行时"}

    missing = missing_modules(python)
    if not missing:
        return {"ok": True, "state": "complete", "missing": []}

    res = install_missing(python, missing, on_line=on_line)
    if not res.get("ok"):
        return {"ok": False, "state": "failed",
                "installed": res.get("installed", []),
                "error": res.get("error")}
    # verify
    still = missing_modules(python)
    return {"ok": not still, "state": "installed" if not still else "failed",
            "missing": still}
