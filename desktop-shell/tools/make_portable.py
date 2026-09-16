"""Assemble the offline portable distribution as a single zip.

    dist/EduBuddyPortable.zip
        EduBuddy/EduBuddyDesktop.exe
        EduBuddy/assets/icon.ico
        EduBuddy/runtime/python/...   # embeddable python + deeptutor
        EduBuddy/runtime/node/...     # portable Node.js

Unzip anywhere, then double-click EduBuddyDesktop.exe. The shell looks for the
runtime/ tree right next to the exe (runtime.py: EXE_DIR/runtime), so the zip is
written directly from runtime-build/staging with a runnable top-level layout.

The zip is compressed (ZIP_DEFLATED) by default so the shipped package is far
smaller than the on-disk runtime. Use --stored to match the old behaviour.
"""
from __future__ import annotations

import argparse
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
PORTABLE = DIST / "portable"
STAGING = ROOT / "runtime-build" / "staging"
PREFIX = "EduBuddy"  # top-level folder inside the zip


def _newest_exe() -> Path:
    candidates = list(DIST.glob("EduBuddyDesktop.exe")) + [
        p for p in DIST.parent.glob("dist*/EduBuddyDesktop.exe")
        if p != DIST / "EduBuddyDesktop.exe"
    ]
    if not candidates:
        return DIST / "EduBuddyDesktop.exe"
    return max(candidates, key=lambda p: p.stat().st_mtime)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default=None,
                    help="path to EduBuddyDesktop.exe (default: newest under dist*/)")
    ap.add_argument("--stored", action="store_true",
                    help="store without compression (fast, much bigger zip)")
    ap.add_argument("--level", type=int, default=6, help="deflate level 0-9")
    ap.add_argument("--with-dir", action="store_true",
                    help="also mirror the tree into dist/portable/ (slow copy)")
    args = ap.parse_args()

    exe = Path(args.exe) if args.exe else _newest_exe()
    if not exe.exists():
        raise SystemExit(f"{exe} not found — run PyInstaller first.")
    assert (STAGING / "python" / "python.exe").exists(), "runtime staging missing"
    assert (STAGING / "node" / "node.exe").exists(), "node staging missing"
    print(f"using shell exe: {exe}")

    mode = zipfile.ZIP_STORED if args.stored else zipfile.ZIP_DEFLATED
    level = 0 if args.stored else args.level
    out_zip = DIST / "EduBuddyPortable.zip"
    out_zip.unlink(missing_ok=True)
    nfiles = 0
    with zipfile.ZipFile(out_zip, "w", mode, compresslevel=level) as zf:
        zf.write(exe, f"{PREFIX}/{exe.name}")
        zf.write(ROOT / "assets" / "icon.ico", f"{PREFIX}/assets/icon.ico")
        nfiles += 2
        for part in ("python", "node"):
            base = Path("runtime") / part
            for f in sorted((STAGING / part).rglob("*")):
                if f.is_file():
                    zf.write(f, f"{PREFIX}/{base.as_posix()}/{f.relative_to(STAGING / part).as_posix()}")
                    nfiles += 1
    print(f"zip at     : {out_zip} "
          f"({out_zip.stat().st_size/1e6:.0f} MB, {nfiles} files, "
          f"{'STORED' if args.stored else 'deflate-' + str(level)})")

    if args.with_dir:
        if PORTABLE.exists():
            shutil.rmtree(PORTABLE, ignore_errors=True)
        PORTABLE.mkdir(parents=True, exist_ok=True)
        shutil.copy2(exe, PORTABLE / exe.name)
        (PORTABLE / "assets").mkdir(exist_ok=True)
        shutil.copy2(ROOT / "assets" / "icon.ico", PORTABLE / "assets" / "icon.ico")
        for part in ("python", "node"):
            shutil.copytree(STAGING / part, PORTABLE / "runtime" / part)
        print(f"portable dir at: {PORTABLE}")


if __name__ == "__main__":
    main()
