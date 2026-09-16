"""Capture the running EduBuddy window to assets/window-shot.png."""
import ctypes
import time
from ctypes import wintypes
from pathlib import Path

from PIL import ImageGrab

user32 = ctypes.windll.user32
hwnd = user32.FindWindowW(None, "EduBuddy")
if not hwnd:
    print("WINDOW NOT FOUND")
    raise SystemExit(1)

rect = wintypes.RECT()
user32.GetWindowRect(hwnd, ctypes.byref(rect))
user32.SetForegroundWindow(hwnd)
time.sleep(0.8)
L, T, R, B = rect.left, rect.top, rect.right, rect.bottom
print("rect:", L, T, R, B)
img = ImageGrab.grab(bbox=(L, T, R, B), all_screens=False)
out = Path(__file__).resolve().parents[1] / "assets" / "window-shot.png"
img.save(out)
print("SAVED", out, img.size)
