"""Every operation in the document is reachable, under the name the document gives it.

The point of generating the SDK is that this cannot drift: an endpoint nobody
wired up fails here rather than three releases later.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from conftest import SPEC, operations, resolve

from talqing import AsyncTalqing, Talqing

CASES = operations()


@pytest.mark.parametrize("operation_id", [oid for oid, _ in CASES])
def test_every_operation_is_reachable(
    client: Talqing, async_client: AsyncTalqing, operation_id: str
) -> None:
    assert callable(resolve(client, operation_id))
    assert callable(resolve(async_client, operation_id))


@pytest.mark.parametrize("operation_id,node", CASES)
def test_every_parameter_is_an_argument(
    client: Talqing, operation_id: str, node: dict[str, Any]
) -> None:
    signature = inspect.signature(resolve(client, operation_id))
    expected = {parameter["name"] for parameter in node.get("parameters", [])}
    body = node.get("requestBody")
    if body:
        ref = body["content"]["application/json"]["schema"]["$ref"].rsplit("/", 1)[-1]
        expected |= set(SPEC["components"]["schemas"][ref].get("properties") or {})
    assert expected == set(signature.parameters)


def test_a_streaming_operation_is_not_a_coroutine(async_client: AsyncTalqing) -> None:
    """`async for` should read the way `for` does, with no await in front of it."""
    assert not inspect.iscoroutinefunction(async_client.conversations.events)
    assert inspect.iscoroutinefunction(async_client.conversations.get)


def test_the_python_keyword_is_the_only_operation_named_anything_else() -> None:
    """Snake-casing is the whole translation, with one exception the language forces."""
    from talqing_generator import attribute, snake

    leaves = [(oid, oid.rsplit(".", 1)[-1]) for oid, _ in CASES]
    assert {
        oid: attribute(leaf) for oid, leaf in leaves if attribute(leaf) != snake(leaf)
    } == {"telephony.phoneNumbers.import": "import_"}


def test_the_checked_in_tree_is_what_the_document_generates() -> None:
    """The freshness gate, so a stale `gen/` fails here as well as in pre-commit."""
    from talqing_generator import build, formatted

    for path, source in formatted(build()).items():
        assert path.read_text() == source, f"{path.name} is stale — run generate.py"
