"""Generate the EduBuddy icon: assets/icon.png + assets/icon.ico.

Sources the artwork from the provided image (the white-cat-graduation-cap
mascot on a green octagonal tile) and converts it into a Windows .ico that
bundles 256/64/48/32/16 for the installer + exe resources.

The source is a full-bleed opaque RGB PNG with white corner triangles
around an octagonal tile. White corner triangles are made fully transparent
(via flood-fill from the four corners, which leaves the white cat body in the
center intact because it is separated by the green tile), giving a clean
rounded-octagon icon with no visible white box.

Usage:
    python tools/make_icon.py [path/to/source.png]

Default source (must exist):
    WORKBUDDY\\2026-09-11-17-50-07\\generated-images\\EduBuddy_whstyle_01_cap.png
"""
from __future__ import annotations

import sys
from collections import deque
from pathlib import Path

from PIL import Image

SIZE = 512  # final icon.png edge length (downscaled from 1024 source)
SRC_ICO = Path(
    r"C:\Users\frank\WorkBuddy\2026-09-11-17-50-07\generated-images\EduBuddy_whstyle_01_cap.png"
)

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
ASSETS.mkdir(exist_ok=True)


def _is_whiteish(rgb: tuple[int, int, int], tol: int = 16) -> bool:
    """True for pixels close to white (the corner triangles in the source)."""
    r, g, b = rgb
    return r > 255 - tol and g > 255 - tol and b > 255 - tol


def make_transparent_octagon(src: Path) -> Image.Image:
    """Load the source, clear the white corner triangles, return RGBA canvas."""
    im = Image.open(src).convert("RGBA")
    w, h = im.size
    px = im.load()

    # --- Flood-fill from the 4 corners, clearing only white-connected pixels ---
    # BFS with ordering-by-value dedupe via a set of visited coords.
    cleared = set()
    queue = deque()
    # seed: all white pixels along the four outer edges (guaranteed corner reach)
    for x in range(w):
        for y in (0, h - 1):
            if _is_whiteish(px[x, y][:3]):
                queue.append((x, y))
                cleared.add((x, y))
    for y in range(h):
        for x in (0, w - 1):
            if _is_whiteish(px[x, y][:3]) and (x, y) not in cleared:
                queue.append((x, y))
                cleared.add((x, y))

    while queue:
        x, y = queue.popleft()
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < w and 0 <= ny < h and (nx, ny) not in cleared:
                if _is_whiteish(px[nx, ny][:3]):
                    cleared.add((nx, ny))
                    queue.append((nx, ny))

    # --- Set alpha=0 on cleared corner pixels ---
    rgba = []
    for y in range(h):
        row = []
        for x in range(w):
            r, g, b, a = px[x, y]
            if (x, y) in cleared:
                row.append((r, g, b, 0))
            else:
                row.append((r, g, b, a))
        rgba.append(row)

    out = Image.new("RGBA", (w, h))
    out.putdata([p for row in rgba for p in row])

    # Downscale to the canonical 512px asset, high quality.
    out = out.resize((SIZE, SIZE), Image.LANCZOS)
    return out


def main() -> None:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else SRC_ICO
    if not src.exists():
        sys.exit(f"source image not found: {src}")

    img = make_transparent_octagon(src)
    img.save(ASSETS / "icon.png")

    # Multi-res .ico (Windows needs a 256 base + common sizes)
    ico_sizes = [256, 64, 48, 32, 16]
    with (ASSETS / "icon.ico").open("wb") as f:
        img.resize((256, 256), Image.LANCZOS).save(
            f, format="ICO", sizes=[(s, s) for s in ico_sizes]
        )
    print(f"wrote {ASSETS / 'icon.png'} from {src.name}")
    print(f"wrote {ASSETS / 'icon.ico'} sizes={ico_sizes}")


if __name__ == "__main__":
    main()
