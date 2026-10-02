#!/usr/bin/env python3
"""Regenerate every derived brand asset from talqing-icon.png.

Run it after replacing the master:

    python3 brand/build.py

The master is the only file anyone should edit by hand. Everything this script
writes is derived, so a size or format change belongs here rather than in a
one-off resize.
"""

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
MASTER = ROOT / "brand" / "talqing-icon.png"

# Each derived PNG exists for one consumer, named where it is wired up:
#   32   the favicon browsers show in a tab
#   180  the icon iOS uses when the site is added to a home screen
#   192  the high-DPI favicon, and the mark rendered in the app's own UI
DERIVED = {
    ROOT / "frontend" / "public" / "brand" / "icon-32.png": 32,
    ROOT / "frontend" / "public" / "brand" / "icon-180.png": 180,
    ROOT / "frontend" / "public" / "brand" / "icon-192.png": 192,
    # Mintlify resizes this itself, so it gets the largest sensible source.
    ROOT / "documentation" / "favicon.png": 256,
}

# Requested by name at the site root by crawlers and older browsers, which never
# read the <link> tags. Three sizes so none of them has to scale it.
ICO = ROOT / "frontend" / "public" / "favicon.ico"
ICO_SIZES = [(16, 16), (32, 32), (48, 48)]


def main() -> None:
    master = Image.open(MASTER).convert("RGBA")
    if master.size != (1024, 1024):
        raise SystemExit(f"master is {master.size}, expected 1024x1024")

    for path, size in DERIVED.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        master.resize((size, size), Image.LANCZOS).save(path, "PNG", optimize=True)
        print(f"{path.relative_to(ROOT)}  {size}x{size}")

    master.resize((48, 48), Image.LANCZOS).save(ICO, "ICO", sizes=ICO_SIZES)
    print(f"{ICO.relative_to(ROOT)}  {', '.join(f'{w}x{h}' for w, h in ICO_SIZES)}")


if __name__ == "__main__":
    main()
