"""Tiny shared API primitives reused across routers (response shapes + helpers),
so each route file doesn't redefine its own copy."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Generic, TypeVar

from fastapi import HTTPException
from pydantic import BaseModel

T = TypeVar("T")
U = TypeVar("U")

# Open JSON object surfaces (tenant userdata, metadata bags, raw provider
# payloads). Prefer a named model when the shape is known; use this only when
# the product intentionally stores arbitrary key/value data.
JsonObject = dict[str, Any]


class OkResponse(BaseModel):
    """The generic '{ok: true}' body returned by deletes / no-content mutations."""

    ok: bool = True


class Page(BaseModel, Generic[T]):
    """Envelope for list endpoints. Carries the page of items plus
    `has_more` so a caller knows another page exists — without paying for a
    `count(*)` over the whole table on every list (compute it by fetching
    `limit + 1` rows and trimming the extra).

    Never return a bare JSON array for collections — root arrays cannot grow
    (pagination fields, totals, …) without a breaking change.
    """

    items: list[T]
    has_more: bool
    limit: int
    offset: int


def page_slice(rows: list[T], *, limit: int, offset: int) -> Page[T]:
    """Build a ``Page`` from an over-fetched sequence (``limit + 1`` rows)."""
    has_more = len(rows) > limit
    return Page(items=rows[:limit], has_more=has_more, limit=limit, offset=offset)


# One model rather than one per resource: "check this draft without publishing
# it" is the same question of an agent and of a tool, and both SDKs already
# model it as a single type. Two copies also drifted — one declared a default
# for `errors`, the other did not.
class ValidateResponse(BaseModel):
    """What every `validate_*` answers. `errors` block a publish; `warnings` do not."""

    # Both required, though every construction site passes both anyway: a
    # default here would only reach the OpenAPI document, where it says the
    # field may be absent from a response that always carries it — and every
    # generated client would then make callers narrow a list that is never null.
    errors: list[str]
    warnings: list[str]


class ErrorBody(BaseModel):
    """The structured half of our error contract: a human `message` plus a list
    of individual `errors` (used wherever we surface many problems at once,
    including tool publish, op-tree checks, and request validation). The global
    exception handlers normalize every API error to
    `{detail: {message, errors}}`, so SDKs can parse one shape consistently."""

    message: str
    # Required for the reason the docstring gives: one shape, parsed without
    # branching. A default would publish `errors` as optional and hand every
    # SDK an `errors?: string[]` to guard, on the one response type that is
    # touched by every failure path there is.
    errors: list[str]


# Published on every operation, so its docstring is customer-facing: it is the
# error type a generated SDK is named after. It exists as a model rather than a
# literal in the spec so the document and the handlers cannot drift.
class ErrorResponse(BaseModel):
    """The body of every error response, at every status code."""

    detail: ErrorBody


def validation_error(errors: list[str], message: str = "validation failed") -> HTTPException:
    """Raise a 400 whose `detail` is the standard {message, errors} shape."""
    return HTTPException(
        status_code=400, detail=ErrorBody(message=message, errors=errors).model_dump()
    )


def field_errors(errors: Iterable[Mapping[str, Any]]) -> list[str]:
    """Render pydantic errors as one readable `field: problem` line each.

    Used by the request-validation handler and by any other caller that
    validates a model outside the request cycle, so a client never has to parse
    two different renderings of the same failure.
    """

    def one(error: Mapping[str, Any]) -> str:
        message = error["msg"]
        # pydantic prefixes custom validator messages with "Value error, "
        if message.startswith("Value error, "):
            message = message[len("Value error, ") :]
        location = ".".join(
            str(part) for part in error.get("loc", ()) if part not in ("body", "query", "path")
        )
        return f"{location}: {message}" if location else message

    return [one(error) for error in errors]


def coalesce(new: U | None, current: T) -> T | U:
    """Partial-update (PATCH) merge: take the patch value when the caller sent
    one, else keep what's stored. (PATCH can't tell 'set to null' from 'absent';
    by convention absent wins — pass a sentinel if you ever need explicit null.)"""
    return current if new is None else new


def mask_tail(plain: str, prefix: str = "") -> str:
    """A safe display hint for a secret: an optional prefix + the last 4 chars
    (e.g. '…ab12' for a tool secret, 'whsec_…ab12' for a webhook signing key).
    Never reveals enough to reconstruct the value."""
    return f"{prefix}…{plain[-4:]}" if len(plain) >= 4 else f"{prefix}…"
