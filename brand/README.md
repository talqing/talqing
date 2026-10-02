# Brand assets

`talqing-icon.png` is the master: the app icon at 1024×1024 — a white speech
bubble trailing into a waveform, on the `ink` black the rest of the product uses
(`#0f0f10`), with the rounded corners carried as transparency so it can sit on
any background without a square behind it. It is the only file here anyone edits
by hand.

Everything else is derived. After replacing the master, run:

```bash
python3 brand/build.py
```

which rewrites `frontend/public/brand/icon-{32,180,192}.png`,
`frontend/public/favicon.ico` and `documentation/favicon.png`. Where each of
those is wired up is a comment in `build.py`.

Two things the script does not touch, because they are not resizes:

- `frontend/app/opengraph-image.png` — the social card, a screenshot of
  `social-card.html`. Open that file in Chrome at a 1200×630 viewport, device
  pixel ratio 2, and save the viewport screenshot over the PNG. It reads the
  master directly, so a new mark only needs a new screenshot.
- The `Talqing` wordmark beside the mark, which is live text everywhere it
  appears (`Logo` in `frontend/app/marketing-chrome.tsx`, the sidebar link in
  `components/Nav.tsx`, the docs navbar via `name` in `documentation/docs.json`)
  rather than an image, so it stays crisp and picks up the page's own font.

## Provenance

The master was cut from a generated render of the icon floating on white, where
the box was blue: it occupied x 149–1258, y 150–1267 of a 1408px JPEG, and its
corners fit a circle of radius 143 — 12.9% of the box width — to within a pixel.
That crop was squared to 1024 and masked to those corners, after the outermost
band of pixels was flooded with the box's own colour, because JPEG had blended
them with the white behind and they would otherwise have ringed the icon in grey.

The blue then became black. Because the render is white ink over a single
background colour, each pixel resolves into how much white ink it carries: fit
the background's soft top-to-bottom gradient, project every pixel onto the line
from that background to white, and repaint the result over `#0f0f10`. The ends
of that coverage were clipped so the background lands on exactly the ink colour
and the ink on exactly white — JPEG speckle that blue had hidden is plainly
visible against black.
