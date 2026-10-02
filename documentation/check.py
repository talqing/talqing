#!/usr/bin/env python3
"""Check the docs site before publishing: pages, frontmatter, links, components.

Run from the repo root:

    python documentation/check.py

Exit code is 1 if anything failed, so this works as a pre-publish gate.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

DOCS = Path(__file__).resolve().parent
CONFIG = json.loads((DOCS / "docs.json").read_text())

# Components Mintlify gives us, plus the HTML we allow inside MDX.
KNOWN_COMPONENTS = {
    "Note",
    "Warning",
    "Tip",
    "Info",
    "Check",
    "Danger",
    "Steps",
    "Step",
    "CodeGroup",
    "Card",
    "CardGroup",
    "Columns",
    "Accordion",
    "AccordionGroup",
    "Tabs",
    "Tab",
    "ParamField",
    "ResponseField",
    "Expandable",
    "Frame",
    "Icon",
    "Update",
    "Tooltip",
    "Latex",
    "Mermaid",
    "br",
    "img",
    "sup",
    "sub",
    "kbd",
    "abbr",
}

problems: list[str] = []


def declared_pages() -> list[str]:
    pages: list[str] = []
    for tab in CONFIG["navigation"]["tabs"]:
        for group in tab["groups"]:
            pages.extend(group.get("pages", []))
    return pages


DECLARED = declared_pages()
DECLARED_SET = set(DECLARED)


def strip_code(text: str) -> str:
    """Remove fenced and inline code so we do not lint examples."""
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    return re.sub(r"`[^`\n]*`", "", text)


# 1. Every declared page exists, and every file on disk is declared.
on_disk = {
    str(path.relative_to(DOCS).with_suffix("")).replace("\\", "/")
    for path in DOCS.rglob("*.mdx")
}

for page in DECLARED:
    if page not in on_disk:
        problems.append(f"declared in docs.json but missing on disk: {page}.mdx")

for page in sorted(on_disk - DECLARED_SET):
    problems.append(f"on disk but not in docs.json navigation: {page}.mdx")

if len(DECLARED) != len(DECLARED_SET):
    seen: set[str] = set()
    for page in DECLARED:
        if page in seen:
            problems.append(f"listed twice in docs.json navigation: {page}")
        seen.add(page)

# 2. Frontmatter, links and components, per page.
LINK = re.compile(r"\]\((/[^)\s#]*)(#[^)\s]*)?\)")
JSX_OPEN = re.compile(r"<([A-Za-z][A-Za-z0-9]*)[\s/>]")
HREF = re.compile(r'href="(/[^"#]*)(?:#[^"]*)?"')
HEADING = re.compile(r"^#{2,6}\s+(.+?)\s*$", re.M)

# Tags in the OpenAPI document become the first path segment of every
# generated endpoint page.
_spec = DOCS / "openapi.json"
OPENAPI_TAGS = (
    {tag["name"] for tag in json.loads(_spec.read_text()).get("tags", [])}
    if _spec.exists()
    else set()
)


def slug(heading: str) -> str:
    """Mintlify's heading anchor: lowercased, punctuation dropped, spaces hyphenated."""
    text = re.sub(r"`|\*\*|\*|_", "", heading)
    text = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[\s]+", "-", text.strip())


# Anchors each page offers, so a cross-page link to one can be checked.
# Fenced blocks only: a heading may legitimately contain inline code, and
# stripping it first would slug `### Where `console` output goes` as
# "where-output-goes".
anchors: dict[str, set[str]] = {}
for page in sorted(on_disk):
    body = re.sub(r"```.*?```", "", (DOCS / f"{page}.mdx").read_text(), flags=re.DOTALL)
    anchors[page] = {slug(match.group(1)) for match in HEADING.finditer(body)}

for page in sorted(on_disk):
    path = DOCS / f"{page}.mdx"
    text = path.read_text()

    if not text.startswith("---\n"):
        problems.append(f"{page}: no frontmatter")
        continue
    end = text.find("\n---", 4)
    if end == -1:
        problems.append(f"{page}: unterminated frontmatter")
        continue
    front, body = text[4:end], text[end + 4 :]

    if not re.search(r"^title:\s*\S", front, re.M):
        problems.append(f"{page}: frontmatter has no title")
    description = re.search(r"^description:\s*\"?(.+?)\"?\s*$", front, re.M)
    if not description:
        problems.append(f"{page}: frontmatter has no description")
    elif len(description.group(1)) > 140:
        problems.append(
            f"{page}: description is {len(description.group(1))} chars (max 140)"
        )

    prose = strip_code(body)

    # Markdown links and JSX href attributes both point at pages, and both
    # break the same way. <Card href="/x"> is invisible to the markdown regex.
    targets = [(m.group(1), m.group(2)) for m in LINK.finditer(prose)]
    targets += [(m.group(1), None) for m in HREF.finditer(prose)]

    for match in targets:
        target = match[0].lstrip("/")
        # Mintlify generates /api-reference/<tag>/<slug> from the OpenAPI
        # document. The slug is title-derived and not worth reproducing, but the
        # tag is checkable — which catches the prefix being wrong entirely.
        parts = target.split("/")
        if len(parts) == 3 and parts[0] == "api-reference" and parts[1] in OPENAPI_TAGS:
            continue
        if target not in DECLARED_SET:
            problems.append(f"{page}: link to unknown page /{target}")
        elif match[1] and target in anchors:
            anchor = match[1].lstrip("#")
            if anchor not in anchors[target]:
                problems.append(f"{page}: link to /{target}#{anchor} — no such heading")

    for match in JSX_OPEN.finditer(prose):
        tag = match.group(1)
        if tag not in KNOWN_COMPONENTS and tag[0].isupper():
            problems.append(f"{page}: unknown component <{tag}>")

    if body.count("```") % 2:
        problems.append(f"{page}: odd number of code fences")

    # MDX reads a bare `{` in prose as the start of a JSX expression, so an
    # unbackticked {{userdata.name}} fails the build. These docs are full of
    # template syntax, so this is the likeliest way to break the site.
    for line_number, line in enumerate(prose.split("\n"), 1):
        if "{" in line and not re.fullmatch(r"\s*(<[A-Za-z].*|/?>)\s*", line):
            snippet = line.strip()[:60]
            problems.append(f"{page}: unbackticked '{{' in prose — {snippet!r}")

    words = len(prose.split())
    if words < 150:
        problems.append(f"{page}: only {words} words — likely a stub")

# 3. The OpenAPI document the API Reference tab is generated from.
openapi = DOCS / "openapi.json"
if not openapi.exists():
    problems.append("openapi.json missing — run documentation/sync-openapi.py")
else:
    document = json.loads(openapi.read_text())
    if not document.get("servers"):
        problems.append(
            "openapi.json has no servers — the API playground will not work"
        )

print(f"{len(DECLARED)} pages declared, {len(on_disk)} on disk")
if problems:
    print(f"\n{len(problems)} problems:\n")
    for problem in problems:
        print(f"  {problem}")
    sys.exit(1)
print("all checks passed")
