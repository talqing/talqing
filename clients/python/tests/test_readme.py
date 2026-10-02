"""Every call the README shows, checked against the real signatures.

The README is what someone copies first, and the TypeScript one went three
releases describing a method the generated surface had never had. Nothing
catches that but a test that reads the file.

This binds rather than sends: a renamed method, a dropped resource or a
parameter that changed name fails here. What the arguments *mean* is
test_requests.py's job.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

import pytest

from talqing import AsyncTalqing, Talqing

README = Path(__file__).resolve().parents[1] / "README.md"
# The name the README binds a client to, sync and async alike.
CLIENT = "talqing"


def snippets() -> list[str]:
    lines = README.read_text().splitlines()
    blocks, current = [], None
    for line in lines:
        if line.startswith("```python"):
            current = []
        elif line.startswith("```") and current is not None:
            blocks.append("\n".join(current))
            current = None
        elif current is not None:
            current.append(line)
    return blocks


def calls() -> list[tuple[str, int, tuple[str, ...]]]:
    """(dotted path, positional count, keyword names) for every client call."""
    found: set[tuple[str, int, tuple[str, ...]]] = set()
    for block in snippets():
        for node in ast.walk(ast.parse(block)):
            if not isinstance(node, ast.Call):
                continue
            path: list[str] = []
            target: ast.expr = node.func
            while isinstance(target, ast.Attribute):
                path.insert(0, target.attr)
                target = target.value
            if not isinstance(target, ast.Name) or target.id != CLIENT or not path:
                continue
            keywords = tuple(k.arg or "**" for k in node.keywords)
            found.add((".".join(path), len(node.args), keywords))
    return sorted(found)


CASES = calls()


def test_the_readme_shows_the_client_being_used() -> None:
    """A README that stopped showing any call would pass every case below."""
    assert len(CASES) >= 12


@pytest.mark.parametrize("path,positional,keywords", CASES)
def test_every_readme_call_binds(
    path: str, positional: int, keywords: tuple[str, ...]
) -> None:
    for client in (
        Talqing(token="x", base_url="https://api.example.com"),
        AsyncTalqing(token="x", base_url="https://api.example.com"),
    ):
        target: Any = client
        for part in path.split("."):
            target = getattr(target, part, None)
            assert target is not None, (
                f"the README calls `{CLIENT}.{path}`, which does not exist"
            )
        inspect.signature(target).bind(
            *[None] * positional, **{k: None for k in keywords}
        )
