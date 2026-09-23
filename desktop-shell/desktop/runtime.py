"""Runtime resolution & provisioning.

EduBuddy Desktop decouples the shell from whatever is installed system-wide.
The managed runtime lives under %LOCALAPPDATA%\\EduBuddy\\runtime:

    runtime/
      venv/   # python venv with `deeptutor` pip-installed (managed, v1)
      node/   # portable Node.js (managed, for installer builds)

Resolution order for each component is: bundled/managed -> PATH system.
This lets us run in dev mode on a machine that already has deeptutor+node,
and run fully self-contained from an installer that ships runtime.zip.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

log = logging.getLogger("dt.runtime")

# -- paths ------------------------------------------------------------------ #
LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
# Overridable for tests / portable (USB) installs.
ROOT = Path(os.environ.get("DEEPTUTOR_DESKTOP_ROOT") or (LOCALAPPDATA / "EduBuddy"))
RUNTIME = ROOT / "runtime"
MANAGED_VENV = RUNTIME / "venv"
NODE_RUNTIME = RUNTIME / "node"
WORKSPACE_HOME = Path(os.environ.get("DEEPTUTOR_DESKTOP_HOME") or (Path.home() / "EduBuddy"))

# Where the PyInstaller bundle keeps an embedded runtime.zip (onefile build)
# or extracted tree (onedir build). sys._MEIPASS works for both.
APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
# Directory holding the unpacked exe (installer layout puts runtime.zip or a
# pre-extracted runtime/ tree right next to the exe).
EXE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else APP_DIR


def _runtime_zip_candidates() -> list[Path]:
    """Where an accompanied runtime.zip may live, in priority order."""
    return [
        EXE_DIR / "runtime.zip",   # installer: next to the exe
        APP_DIR / "runtime.zip",   # onefile build: embedded
        ROOT / "runtime.zip",
    ]


def _runtime_tree_candidates() -> list[Path]:
    """Where a pre-extracted runtime/ tree may already live."""
    return [
        RUNTIME,                   # extracted into LOCALAPPDATA
        EXE_DIR / "runtime",
        APP_DIR / "runtime",
    ]


def app_dir() -> Path:
    return APP_DIR


# -- node ------------------------------------------------------------------- #
def resolve_node_dir() -> Path | None:
    """Return a directory containing node.exe, or None if unavailable."""
    candidates: list[Path] = []
    if NODE_RUNTIME.joinpath("node.exe").exists():
        candidates.append(NODE_RUNTIME)
    if EXE_DIR.joinpath("runtime", "node", "node.exe").exists():
        candidates.append(EXE_DIR / "runtime" / "node")
    if APP_DIR.joinpath("node", "node.exe").exists():
        candidates.append(APP_DIR / "node")
    for cand in candidates:
        log.info("using managed node at %s", cand)
        return cand
    which = shutil.which("node")
    if which:
        log.info("using node from PATH: %s", which)
        return Path(which).parent
    log.warning("node.js not found anywhere")
    return None


# -- deeptutor -------------------------------------------------------------- #
def deeptutor_binary_name() -> str:
    return "deeptutor.exe" if os.name == "nt" else "deeptutor"


def _path_or_none(p: Path) -> Path | None:
    return p if p.exists() else None


def resolve_deeptutor() -> Path | None:
    """Return the path to the deeptutor executable, or None."""
    name = deeptutor_binary_name()
    for cand in (MANAGED_VENV / "Scripts" / name, APP_DIR / name):
        found = _path_or_none(cand)
        if found:
            log.info("using managed deeptutor at %s", found)
            return found
    which = shutil.which("deeptutor")
    if which:
        log.info("using deeptutor from PATH: %s", which)
        return Path(which)
    log.warning("deeptutor executable not found anywhere")
    return None


# Invocation that works against the relocatable embedded runtime:
#   runtime/python/python.exe  run_deeptutor.py  start ...
def resolve_deeptutor_cmd() -> list[str] | None:
    """Return the full argv prefix used to launch deeptutor, or None."""
    # pre-extracted embeddable runtime: managed, exe-adjacent, or bundled
    for base in (RUNTIME, EXE_DIR / "runtime", APP_DIR / "runtime"):
        embed_py = base / "python" / "python.exe"
        if embed_py.exists():
            runner = base / "python" / "run_deeptutor.py"
            log.info("using embedded runtime python: %s", embed_py)
            if runner.exists():
                # -u：stdout 指向日志文件时 Python 按块缓冲，"前端 已就绪" 等
                # 就绪信号会被滞留在缓冲区里，壳的 wait_ready 只能白等超时。
                return [str(embed_py), "-u", str(runner)]
            return [str(embed_py), "-u", "-c",
                    "from deeptutor_cli.main import main; raise SystemExit(main())"]
    exe = resolve_deeptutor()
    return [str(exe)] if exe else None


def resolve_deeptutor_version() -> str | None:
    """Best-effort 读取运行时里 deeptutor 的版本号（不 import，读 dist-info METADATA）。

    供「关于 EduBuddy」等信息展示用。打包版运行时在
    ``<runtime>/python/Lib/site-packages/deeptutor-<ver>.dist-info/METADATA``；
    开发态兜底尝试 import（读 deeptutor.__version__ 子模块）。
    """
    for base in (RUNTIME, EXE_DIR / "runtime", APP_DIR / "runtime"):
        site = base / "python" / "Lib" / "site-packages"
        try:
            if site.exists():
                for dist in sorted(site.glob("deeptutor-*.dist-info")):
                    meta = dist / "METADATA"
                    if not meta.exists():
                        continue
                    for line in meta.read_text(encoding="utf-8", errors="replace").splitlines():
                        if line.startswith("Version:"):
                            version = line.split(":", 1)[1].strip()
                            if version:
                                return version
        except OSError:
            continue
    try:  # 开发态：壳脚本可能跑在已装 deeptutor 的 Python 里
        from deeptutor import __version__ as _sub  # noqa
        return str(getattr(_sub, "__version__", "") or "").strip() or None
    except Exception:  # noqa: BLE001
        return None


# -- provisioning ------------------------------------------------------------ #
def _read_zip_runtime_manifest(zf: zipfile.ZipFile) -> dict | None:
    try:
        value = json.loads(zf.read("runtime-manifest.json"))
    except (KeyError, json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def _read_managed_runtime_manifest() -> dict | None:
    try:
        value = json.loads((RUNTIME / "runtime-manifest.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def extract_bundled_runtime() -> bool:
    """Provision or refresh the managed runtime from the bundled runtime.zip."""
    zips = [z for z in _runtime_zip_candidates() if z.exists()]
    if not zips:
        return False
    src = zips[0]
    try:
        with zipfile.ZipFile(src) as zf:
            incoming = _read_zip_runtime_manifest(zf)
            if RUNTIME.joinpath("python", "python.exe").exists():
                if _read_managed_runtime_manifest() == incoming:
                    return True
                log.info("managed runtime differs from bundled runtime; refreshing")
            else:
                log.info("provisioning managed runtime from %s", src.name)

            # RUNTIME is the shell-owned cache under EduBuddy; the user's
            # learning workspace is WORKSPACE_HOME and is never touched here.
            if RUNTIME.exists():
                shutil.rmtree(RUNTIME)
            RUNTIME.mkdir(parents=True, exist_ok=True)
            zf.extractall(RUNTIME)
        log.info("runtime extracted to %s", RUNTIME)
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("runtime extraction failed: %s", exc)
        shutil.rmtree(RUNTIME, ignore_errors=True)
        return False


def _find_system_python() -> Path | None:
    """Best-effort discovery of a usable python.exe for provisioning."""
    for cand in (shutil.which("python"), shutil.which("python3"), shutil.which("py")):
        if cand:
            p = Path(cand)
            if p.name.lower().startswith("py"):
                return p  # the py launcher; venv creation may need 'py -3'
            return p
    return None


def provision_with_system_python(on_line=None) -> bool:
    """Create/extend the managed venv with deeptutor using a system python.

    Used when no deeptutor is present anywhere but a python3 exists.
    """
    if deeptutor_binary_name() == "deeptutor.exe":
        exe = MANAGED_VENV / "Scripts" / deeptutor_binary_name()
    else:
        exe = MANAGED_VENV / "bin" / deeptutor_binary_name()
    if exe.exists():
        return True
    py = _find_system_python()
    if py is None:
        log.warning("no system python available for provisioning")
        return False

    def say(msg: str) -> None:
        log.info("%s", msg)
        if on_line:
            try:
                on_line(msg)
            except Exception:  # noqa: BLE001
                pass

    try:
        if not MANAGED_VENV.joinpath("pyvenv.cfg").exists():
            say("正在创建运行环境（venv）…")
            subprocess.run(
                [str(py), "-m", "venv", str(MANAGED_VENV)],
                check=True, capture_output=True, timeout=180,
                creationflags=0x08000000 if os.name == "nt" else 0,
            )
        say("正在安装 deeptutor（首次需要联网，请稍候）…")
        pyv = MANAGED_VENV / "Scripts" / "python.exe" if os.name == "nt" \
            else MANAGED_VENV / "bin" / "python"
        subprocess.run(
            [str(pyv), "-m", "pip", "install", "--upgrade", "pip"],
            check=True, capture_output=True, timeout=300,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        subprocess.run(
            [str(pyv), "-m", "pip", "install", "-U", "deeptutor"],
            check=True, capture_output=True, timeout=900,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        say("deeptutor 安装完成 ✓")
        return True
    except Exception as exc:  # noqa: BLE001
        log.exception("provision failed")
        say(f"deeptutor 自动安装失败：{exc}")
        return False


def ensure_runtime(on_line=None) -> tuple[list[str] | None, Path | None]:
    """Make sure the app can run; return (deeptutor_cmd, node_dir).

    1. managed/embedded runtime (installer builds)  -> use it
    2. system deeptutor + system node (dev builds)  -> use them
    3. nothing on PATH -> try provision w/ system python
    """
    extract_bundled_runtime()
    node_dir = resolve_node_dir()

    deeptutor_cmd = resolve_deeptutor_cmd()
    if deeptutor_cmd is None and node_dir is None:
        provision_with_system_python(on_line)
        # after provisioning, re-resolve managed venv exe
        deeptutor_cmd = resolve_deeptutor_cmd()

    # not provisioned -> last-chance system deeptutor
    if deeptutor_cmd is None:
        deeptutor_cmd = resolve_deeptutor_cmd()
    return deeptutor_cmd, node_dir


# -- workspace --------------------------------------------------------------- #
def default_workspace() -> Path:
    return WORKSPACE_HOME


def run_diagnostic(deeptutor: Path) -> list[str]:
    """Best-effort `deeptutor doctor` output for the status screen."""
    try:
        res = subprocess.run(
            [str(deeptutor), "doctor"],
            capture_output=True,
            text=True,
            timeout=60,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        out = (res.stdout or "") + (res.stderr or "")
        return [ln for ln in out.splitlines() if ln.strip()][:12]
    except Exception as exc:  # noqa: BLE001
        return [f"doctor unavailable: {exc}"]
