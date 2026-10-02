#!/usr/bin/env bash
# Build the docs site into a static bundle Cloudflare Pages can serve.
#
# Cloudflare Pages settings:
#   Build command       bash documentation/build.sh
#   Output directory    documentation/dist
#   Root directory      (repo root, the default)
#
# Run it locally the same way to see exactly what will be published.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

# Pinned: an unpinned global install means the site can change without a commit.
MINT_VERSION="4.2.850"

if ! command -v mint >/dev/null 2>&1 || [ "$(mint version 2>/dev/null | head -1)" != "$MINT_VERSION" ]; then
  npm install -g "mint@${MINT_VERSION}"
fi

# Refuse to publish a site that does not pass our own checks — missing pages,
# dead links, unbalanced fences and the bare-`{` MDX hazard all break silently
# once deployed.
python3 check.py

rm -rf dist export.zip
mint export --output export.zip
mkdir -p dist
unzip -q export.zip -d dist
rm -f export.zip

# `mint export` copies every loose file at the docs root into the bundle, so the
# tooling that builds the site would otherwise be served from it. The .mdx pages
# and _research/ are unaffected — Mintlify only exports what docs.json names.
rm -f dist/check.py dist/sync-openapi.py dist/sitemap.py dist/build.sh
rm -f dist/serve.js "dist/Start Docs.command" "dist/Start Docs.bat"

# mint export ships no sitemap.xml, robots.txt or canonical links; generate them from what was
# actually built, so the generated API-reference pages are included too.
python3 sitemap.py

test -f dist/index.html || { echo "build produced no index.html" >&2; exit 1; }
for internal in check.py sync-openapi.py sitemap.py build.sh STYLE.md SITEMAP.md _research; do
  if [ -e "dist/$internal" ]; then
    echo "internal file leaked into the bundle: $internal" >&2
    exit 1
  fi
done

echo "built $(find dist -name '*.html' | wc -l | tr -d ' ') pages into documentation/dist"
