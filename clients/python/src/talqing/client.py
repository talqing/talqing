"""The client itself: credentials, the integration redirect, and pagination.

Every one of the API's operations is generated into `gen/` from
`openapi/openapi.json`. What is left here is only what the document cannot say.
"""

from __future__ import annotations

import os
from types import TracebackType
from typing import Any, AsyncIterator, Awaitable, Callable, Iterator, Mapping, TypeVar
from urllib.parse import quote, urlencode

import httpx

from ._transport import AsyncTransport, Transport
from .gen.api import TalqingApi
from .gen.async_api import AsyncTalqingApi
from .gen.types import Page

T = TypeVar("T")

TOKEN_ENV = "TALQING_API_KEY"
BASE_URL_ENV = "TALQING_BASE_URL"


def _token(value: str | None) -> str:
    if value and value.strip():
        return value.strip()
    raise ValueError(
        "a Talqing personal access token is required: pass token=..., or set "
        f"{TOKEN_ENV} and use Talqing.from_env(). Create one on the dashboard "
        "under Organization → API Tokens."
    )


def _base_url(value: str | None) -> str:
    if value and value.strip():
        return value.strip().rstrip("/")
    # No default, deliberately. A client that quietly points at localhost fails
    # in production as a connection error three layers down; one that refuses to
    # start says what is actually wrong.
    raise ValueError(
        "base_url is required: pass base_url='https://api.in.talqing.com', or "
        f"set {BASE_URL_ENV} and use Talqing.from_env()."
    )


# `include_in_schema=False` in the backend because it is a browser redirect,
# not an operation, so nothing generates it — which is also why the two clients
# must not each carry their own copy of the path.
#
# Google sign-in is NOT here, though it is the same kind of thing: it lives on
# the control API, which this client does not hold the URL of and which is
# published nowhere. A helper building it from a regional `base_url` would
# return a 404.
def _oauth_start_url(base_url: str, provider: str, integration_id: str | None) -> str:
    url = f"{base_url}/v1/integrations/oauth/{quote(provider, safe='')}/start"
    if integration_id:
        url += "?" + urlencode({"integration_id": integration_id})
    return url


def _from_env(overrides: dict[str, Any]) -> dict[str, Any]:
    return {
        "token": os.getenv(TOKEN_ENV),
        "base_url": os.getenv(BASE_URL_ENV),
        **overrides,
    }


class Talqing(TalqingApi):
    """A synchronous client for the Talqing API.

    ```python
    with Talqing(token=..., base_url="https://api.in.talqing.com") as talqing:
        agent = talqing.agents.get(agent_id)
    ```

    Operations are reached by resource, the way `backend/api/sdk_surface.py`
    names them: `talqing.agents.versions.rollback(agent_id, 3)`. Every one of
    them raises `TalqingAPIError` on a non-2xx and returns the decoded body
    otherwise.
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float | httpx.Timeout = 30.0,
        headers: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__(
            Transport(
                base_url=_base_url(base_url),
                token=_token(token),
                timeout=timeout,
                headers=headers,
                transport=transport,
            )
        )

    @classmethod
    def from_env(cls, **overrides: Any) -> "Talqing":
        """Build a client from `TALQING_API_KEY` and `TALQING_BASE_URL`.

        Anything passed here wins over the environment.
        """
        return cls(**_from_env(overrides))

    @property
    def base_url(self) -> str:
        return self._t.base_url

    @property
    def http(self) -> httpx.Client:
        """The underlying `httpx.Client`, already carrying the base URL and token."""
        return self._t.http

    def request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        """Call a path directly, with this client's credentials and error handling.

        The escape hatch for an endpoint that shipped since this SDK was
        generated. Everything the document knows about already has a method.
        """
        return self._t.request(method, path, query=query, body=body)

    def oauth_start_url(self, provider: str, integration_id: str | None = None) -> str:
        """Where to send a browser to authorize an integration."""
        return _oauth_start_url(self.base_url, provider, integration_id)

    def close(self) -> None:
        self._t.close()

    def __enter__(self) -> "Talqing":
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        self.close()


class AsyncTalqing(AsyncTalqingApi):
    """`Talqing`, awaited. See it for the shape; every operation here is a coroutine.

    ```python
    async with AsyncTalqing.from_env() as talqing:
        agents = await talqing.agents.list()
    ```

    The exception is a stream, which is not a coroutine on either client:
    `async for event in talqing.conversations.events(conversation_id)`.
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float | httpx.Timeout = 30.0,
        headers: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(
            AsyncTransport(
                base_url=_base_url(base_url),
                token=_token(token),
                timeout=timeout,
                headers=headers,
                transport=transport,
            )
        )

    @classmethod
    def from_env(cls, **overrides: Any) -> "AsyncTalqing":
        """Build a client from `TALQING_API_KEY` and `TALQING_BASE_URL`."""
        return cls(**_from_env(overrides))

    @property
    def base_url(self) -> str:
        return self._t.base_url

    @property
    def http(self) -> httpx.AsyncClient:
        """The underlying `httpx.AsyncClient`, already carrying the base URL and token."""
        return self._t.http

    async def request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        """Call a path directly, with this client's credentials and error handling."""
        return await self._t.request(method, path, query=query, body=body)

    def oauth_start_url(self, provider: str, integration_id: str | None = None) -> str:
        """Where to send a browser to authorize an integration."""
        return _oauth_start_url(self.base_url, provider, integration_id)

    async def close(self) -> None:
        await self._t.close()

    async def __aenter__(self) -> "AsyncTalqing":
        return self

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        await self.close()


def paginate(
    page: Callable[..., Page[T]], *, limit: int = 200, **filters: Any
) -> Iterator[T]:
    """Walk every page of a list endpoint.

    ```python
    for agent in paginate(talqing.agents.list):
        ...
    for call in paginate(talqing.calls.list, agent_id=agent_id):
        ...
    ```

    One helper rather than a `list_all_*` per endpoint: all 28 list endpoints
    page the same way, so this does too. Anything else the endpoint filters on
    is passed straight through.
    """
    offset = 0
    while True:
        result = page(limit=limit, offset=offset, **filters)
        yield from result["items"]
        if not result["has_more"]:
            return
        offset += limit


async def paginate_async(
    page: Callable[..., Awaitable[Page[T]]], *, limit: int = 200, **filters: Any
) -> AsyncIterator[T]:
    """`paginate`, awaited.

    ```python
    async for agent in paginate_async(talqing.agents.list):
        ...
    ```
    """
    offset = 0
    while True:
        result = await page(limit=limit, offset=offset, **filters)
        for item in result["items"]:
            yield item
        if not result["has_more"]:
            return
        offset += limit
