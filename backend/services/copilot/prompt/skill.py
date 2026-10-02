"""The shared platform knowledge every CoPilot is built on.

``skill.md`` is shared verbatim with external clients through the MCP package —
our CoPilots and a user's own Claude Code or Codex work from the same document.
Only the framing differs: a CoPilot sits inside one resource's editor and is
handed live state each turn.

A CoPilot takes the whole document or the chapters it can act on: ``skill()``
for everything, ``sections(...)`` for a subset.
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

import yaml

SKILL_PATH = Path(__file__).parent.with_name("skill.md")

_HEADING = re.compile(r"^## (.+)$", re.MULTILINE)


@cache
def skill() -> str:
    """The shared agent-building knowledge (also published as mcp/SKILL.md)."""
    return SKILL_PATH.read_text(encoding="utf-8").strip()


@cache
def _chapters() -> dict[str, str]:
    """``skill.md`` split on its ``##`` headings, heading text included.

    Everything before the first ``##`` — the title and preamble — is dropped; a
    subset prompt supplies its own framing.
    """
    text = skill()
    headings = list(_HEADING.finditer(text))
    bounds = [m.start() for m in headings] + [len(text)]
    return {
        m.group(1).strip(): text[bounds[i] : bounds[i + 1]].strip() for i, m in enumerate(headings)
    }


def sections(*titles: str) -> str:
    """The named ``##`` chapters of ``skill.md``, in the order given.

    Raises on an unknown title: renaming a heading must break the build rather
    than silently drop knowledge out of a CoPilot's prompt.
    """
    chapters = _chapters()
    missing = [title for title in titles if title not in chapters]
    if missing:
        raise RuntimeError(
            f"skill.md has no section(s) {', '.join(missing)} — "
            f"known sections are {', '.join(chapters)}"
        )
    return "\n\n".join(chapters[title] for title in titles)


def yaml_block(data: dict) -> str:
    """State blocks are YAML: cheaper to read than JSON, and diff-friendly."""
    return yaml.safe_dump(data, sort_keys=True, allow_unicode=False, width=110)
