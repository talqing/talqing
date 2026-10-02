"""Shared fixtures, and the generator itself.

`generate.py` is a script beside the package rather than a module inside it, so
the tests that check the generated tree against the document load it by path and
register it under a name they can import.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from talqing import AsyncTalqing, Talqing

ROOT = Path(__file__).resolve().parents[1]
SPEC: dict[str, Any] = json.loads(
    (ROOT.parents[1] / "openapi" / "openapi.json").read_text()
)
HTTP_METHODS = ("get", "post", "put", "patch", "delete")

_spec = importlib.util.spec_from_file_location(
    "talqing_generator", ROOT / "generate.py"
)
assert _spec is not None and _spec.loader is not None
_generator = importlib.util.module_from_spec(_spec)
sys.modules["talqing_generator"] = _generator
_spec.loader.exec_module(_generator)


def operations() -> list[tuple[str, dict[str, Any]]]:
    return [
        (node["operationId"], node)
        for item in SPEC["paths"].values()
        for verb, node in item.items()
        if verb in HTTP_METHODS
    ]


def resolve(client: object, operation_id: str) -> Any:
    target: Any = client
    for part in operation_id.split("."):
        target = getattr(target, _generator.attribute(part))
    return target


@pytest.fixture(scope="session")
def client() -> Talqing:
    return Talqing(token="test", base_url="https://api.example.com")


@pytest.fixture(scope="session")
def async_client() -> AsyncTalqing:
    return AsyncTalqing(token="test", base_url="https://api.example.com")
