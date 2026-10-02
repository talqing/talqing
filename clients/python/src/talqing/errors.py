"""The one exception this SDK raises."""

from __future__ import annotations

from typing import Any, Optional

import httpx


class TalqingAPIError(Exception):
    """Every error the API returns, in the one shape it returns them in.

    The wire body is always ``{"detail": {"message", "errors"}}``, so there is
    nothing to branch on: ``str(exc)`` is the message, ``exc.errors`` lists the
    per-field problems when the failure had more than one, and ``exc.detail`` is
    the decoded body for anything else you want off it.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        detail: Any = None,
        errors: Optional[list[str]] = None,
        response: Optional[httpx.Response] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail
        self.errors = errors or []
        self.response = response
