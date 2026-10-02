#!/usr/bin/env python3
"""Write sitemap.xml, robots.txt, _redirects and canonical links into the built docs bundle.

`mint export` ships none of the four. A 130-page site nothing can enumerate is
a site search engines index badly, and a renamed page with no redirect is every
link to it broken.

The first two are generated from `dist/` after the build rather than from
`docs.json`, so the per-endpoint API reference pages Mintlify generates from the
OpenAPI document are included without reproducing its slug rules here.
`_redirects` is the exception and comes from `docs.json`, which is where a
redirect is declared — Mintlify's own hosting reads it from there, and this
translates the same list into the file Cloudflare Pages reads.

The canonical link is written into each page's HTML here, and deliberately not
through `seo.metatags.canonical` in `docs.json`: under `mint export` (4.2.850)
that setting emits `/src/_props/<page>` — a URL that does not exist — on every
page, and a wrong canonical is worse than none.

Run by `build.sh`; there is nothing to do by hand.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

SITE = "https://docs.talqing.com"
HERE = Path(__file__).resolve().parent
DIST = HERE / "dist"

# A fixed date rather than build time, matching the marketing site's reasoning:
# a sitemap whose dates move on every deploy teaches crawlers the dates mean
# nothing. Bump when the documentation actually changes substantially.
LAST_MODIFIED = date(2026, 10, 2).isoformat()


def routes() -> list[str]:
    """Every page in the bundle, as a site-root path. `/` sorts first.

    With a trailing slash, because that is the form the deployed site treats as
    canonical: a request for `/agents/overview` answers `308` to
    `/agents/overview/`. A sitemap of URLs that all redirect is a sitemap of
    non-canonical URLs.
    """
    found = set()
    for index in DIST.rglob("index.html"):
        path = index.parent.relative_to(DIST).as_posix()
        found.add("/" if path == "." else f"/{path}/")
    return sorted(found, key=lambda route: (route != "/", route))


def priority(route: str) -> str:
    if route == "/":
        return "1.0"
    # The tab landing pages a reader is most likely to want ranked.
    if route.count("/") == 2 or route.endswith(("/introduction/", "/overview/")):
        return "0.8"
    return "0.5"


def main() -> int:
    if not DIST.is_dir():
        print("dist/ not found — run build.sh first", file=sys.stderr)
        return 1

    # `mint export` writes the first navigation page a second time as the site
    # root. One of the two has to be the canonical, and the root is the one
    # everything links to; the copy stays reachable but out of the sitemap.
    home = (DIST / "index.html").read_bytes()
    canonical = {
        route: "/"
        if (DIST / route.strip("/") / "index.html").read_bytes() == home
        else route
        for route in routes()
    }
    for route, target in canonical.items():
        page = DIST / route.strip("/") / "index.html"
        html = page.read_text()
        if html.count("</head>") != 1 or 'rel="canonical"' in html:
            print(f"cannot place a canonical link in {page}", file=sys.stderr)
            return 1
        url = escape(SITE + target)
        page.write_text(
            html.replace(
                "</head>",
                f'<link rel="canonical" href="{url}"/><meta property="og:url" content="{url}"/></head>',
            )
        )

    paths = [route for route, target in canonical.items() if route == target]
    urls = "\n".join(
        "  <url>\n"
        f"    <loc>{escape(SITE + route)}</loc>\n"
        f"    <lastmod>{LAST_MODIFIED}</lastmod>\n"
        f"    <priority>{priority(route)}</priority>\n"
        "  </url>"
        for route in paths
    )
    (DIST / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}\n"
        "</urlset>\n"
    )

    # Everything here is public documentation, so the whole site is crawlable.
    (DIST / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\n\nSitemap: {SITE}/sitemap.xml\n"
    )

    # Cloudflare Pages reads `_redirects` from the output directory: one
    # `source destination status` per line. 301 unless `permanent` says
    # otherwise — a renamed page is permanent, and a 302 leaves search engines
    # and bookmarks pointing at the old URL forever.
    #
    # Both source forms are emitted, and the destination is written with a
    # trailing slash, because the deployed site canonicalises to that: without
    # the second line `/old/` 308s to a page that does not exist instead of
    # reaching the redirect, and without the slash on the destination every
    # redirect costs a second hop.
    redirects = json.loads((HERE / "docs.json").read_text()).get("redirects", [])
    lines = []
    for entry in redirects:
        source = entry["source"].rstrip("/")
        destination = entry["destination"]
        if destination.startswith("/") and not destination.endswith("/"):
            destination += "/"
        status = 301 if entry.get("permanent", True) else 302
        lines.append(f"{source} {destination} {status}")
        lines.append(f"{source}/ {destination} {status}")
    (DIST / "_redirects").write_text("\n".join(lines) + "\n" if lines else "")

    print(
        f"wrote sitemap.xml with {len(paths)} urls, {len(canonical)} canonical links, "
        f"robots.txt, and _redirects with {len(redirects)} rules"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
