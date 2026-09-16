# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for EduBuddyDesktop.exe (onefile, windowed).

The exe bundles:
  * desktop shell (pywebview + launcher)  — the actual app
  * assets/icon.ico                        — exe icon
  * dist/runtime.zip (if present)          — offline deeptutor runtime,
                                             extracted on first launch
The shell uses the embedded runtime when present, else the system
deeptutor/node on PATH.
"""
import os
from pathlib import Path

ROOT = Path(SPECPATH)          # build/  (this spec lives in build/)
PROJECT = ROOT.parent
ASSETS = PROJECT / "assets"
DIST = PROJECT / "dist"

datas = [
    (str(ASSETS / "icon.ico"), "assets"),
    (str(ASSETS / "icon.png"), "assets"),
]
runtime_zip = DIST / "runtime.zip"
if runtime_zip.exists():
    datas.append((str(runtime_zip), "."))

a = Analysis(
    [str(PROJECT / "desktop" / "main.py")],
    pathex=[str(PROJECT)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "webview",
        "webview.platforms.winforms",
        "webview.platforms.edgechromium",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "PyQt5", "PySide2", "PySide6", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="EduBuddyDesktop",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,                  # no console window (WorkBuddy-like)
    disable_windowed_traceback=False,
    icon=str(ASSETS / "icon.ico"),
)
