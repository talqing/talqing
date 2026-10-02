"""The half of the SDK the OpenAPI document cannot describe: the HTTP itself.

`gen/` says what every operation is; this says how one is sent. It carries the
credentials, turns a non-2xx into `TalqingAPIError`, reads an event stream, and
holds the sentinel that separates "leave this alone" from "set it to null".
"""

from __future__ import annotations

import json as jsonlib
from types import TracebackType
from typing import Any, AsyncIterator, Generic, Iterator, Literal, Mapping, TypeVar
from urllib.parse import quote

import httpx

from ._version import __version__
from .errors import TalqingAPIError

T = TypeVar("T")

Expect = Literal["json", "none", "bytes"]


class _Omit:
    """The absence of an argument, which is not the same as `None`.

    Several PATCH bodies take `null` as a real value — `orgs.update(
    retention_days=None)` means "keep everything forever" — so a default of
    `None` would leave "leave the policy alone" unsayable. An argument still at
    `OMIT` is not sent at all, and the server applies its own default.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "OMIT"

    def __bool__(self) -> bool:
        return False


# Typed `Any` deliberately: this is the default of every optional argument in
# `gen/`, and typing it as itself would put `| _Omit` in three hundred
# signatures for no reader's benefit.
OMIT: Any = _Omit()


def encode_path(value: Any) -> str:
    """One path segment, escaped. `safe=""` because a path parameter is never a path."""
    return quote(str(value), safe="")


def sent(values: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Drop the arguments the caller never passed."""
    if values is None:
        return None
    return {key: value for key, value in values.items() if value is not OMIT}


def raise_for_status(response: httpx.Response) -> None:
    if response.is_success:
        return
    # The contract is strict: the body is always {"detail": {"message", "errors"}}.
    # The fallback below is for a failure that never reached the API at all — a
    # proxy's HTML 502, a gateway timeout — not for a second response shape.
    detail: Any
    errors: list[str] = []
    try:
        detail = response.json()["detail"]
        message = detail["message"]
        errors = [str(error) for error in detail["errors"]]
    except (ValueError, KeyError, TypeError):
        detail = response.text or response.reason_phrase
        message = detail
    raise TalqingAPIError(
        message,
        status_code=response.status_code,
        detail=detail,
        errors=errors,
        response=response,
    )


class _Decoder:
    """The `text/event-stream` state machine, fed one line at a time.

    A class rather than a generator because `async for` cannot drive a
    synchronous one, and the parsing itself must not be written twice.

    The `event:` line is not read. Every frame repeats its own name in the
    JSON's `event` field — what the unions in `gen/types.py` discriminate on —
    so reading it twice would only create somewhere for the two to disagree.
    """

    def __init__(self) -> None:
        self._data: list[str] = []

    def feed(self, line: str) -> list[Any]:
        """The frames this line completed: none, or one. A list, so "not yet"
        needs no sentinel to tell it from a frame that decoded to `None`."""
        if line == "":
            return self.flush()
        if line.startswith(":"):
            return []  # a keep-alive, sent every 15s so idle proxies hold on
        field, _, value = line.partition(":")
        if field == "data":
            self._data.append(value[1:] if value.startswith(" ") else value)
        return []

    def flush(self) -> list[Any]:
        if not self._data:
            return []
        frame = jsonlib.loads("\n".join(self._data))
        self._data = []
        return [frame]


def _options(expect: Expect) -> dict[str, Any]:
    """The request options that depend on what is coming back.

    The two recording endpoints answer `302` and the media is what the caller
    asked for, so those follow the redirect. Nothing else in the API redirects,
    and a POST that silently followed one would be a surprise worth avoiding.
    httpx drops the Authorization header when a redirect leaves our origin,
    which is what makes handing the request on to object storage safe.
    """
    binary = expect == "bytes"
    return {
        "headers": {"Accept": "*/*" if binary else "application/json"},
        "follow_redirects": binary,
    }


def _decode(response: httpx.Response, expect: Expect) -> Any:
    if expect == "none":
        return None
    if expect == "bytes":
        return response.content
    return response.json()


def _headers(token: str, extra: Mapping[str, str] | None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": f"talqing-python/{__version__}",
    }
    headers.update(extra or {})
    return headers


class Stream(Generic[T]):
    """An open `text/event-stream`, as an iterator of decoded frames.

    Iterating opens the connection and closes it when the stream ends or the
    loop is left. Use it as a context manager when the loop may exit early::

        with talqing.conversations.events(conversation_id) as events:
            for event in events:
                if event["event"] == "turn":
                    break
    """

    def __init__(
        self, client: httpx.Client, url: str, params: Mapping[str, Any] | None
    ) -> None:
        self._context = client.stream(
            "GET", url, params=params, headers={"Accept": "text/event-stream"}
        )
        self._response: httpx.Response | None = None
        self._spent = False

    def __enter__(self) -> "Stream[T]":
        self._open()
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        self.close()

    def __iter__(self) -> Iterator[T]:
        self._open()
        assert self._response is not None
        decoder = _Decoder()
        try:
            for line in self._response.iter_lines():
                yield from decoder.feed(line)
            yield from decoder.flush()
        finally:
            self.close()

    def _open(self) -> None:
        if self._response is not None:
            return
        if self._spent:
            raise RuntimeError("this stream is finished; open a new one to watch again")
        self._response = self._context.__enter__()
        if not self._response.is_success:
            # Nothing has been read yet, so the error body is still on the wire.
            self._response.read()
            try:
                raise_for_status(self._response)
            finally:
                self.close()

    def close(self) -> None:
        if self._response is None:
            return
        self._response = None
        self._spent = True
        self._context.__exit__(None, None, None)


class AsyncStream(Generic[T]):
    """`Stream`, awaited. See it for the shape; this differs only in `async for`."""

    def __init__(
        self, client: httpx.AsyncClient, url: str, params: Mapping[str, Any] | None
    ) -> None:
        self._context = client.stream(
            "GET", url, params=params, headers={"Accept": "text/event-stream"}
        )
        self._response: httpx.Response | None = None
        self._spent = False

    async def __aenter__(self) -> "AsyncStream[T]":
        await self._open()
        return self

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def __aiter__(self) -> AsyncIterator[T]:
        await self._open()
        assert self._response is not None
        decoder = _Decoder()
        try:
            async for line in self._response.aiter_lines():
                for frame in decoder.feed(line):
                    yield frame
            for frame in decoder.flush():
                yield frame
        finally:
            await self.aclose()

    async def _open(self) -> None:
        if self._response is not None:
            return
        if self._spent:
            raise RuntimeError("this stream is finished; open a new one to watch again")
        self._response = await self._context.__aenter__()
        if not self._response.is_success:
            await self._response.aread()
            try:
                raise_for_status(self._response)
            finally:
                await self.aclose()

    async def aclose(self) -> None:
        if self._response is None:
            return
        self._response = None
        self._spent = True
        await self._context.__aexit__(None, None, None)


class Transport:
    """One configured `httpx.Client`, and the two things every method needs."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout: float | httpx.Timeout,
        headers: Mapping[str, str] | None,
        transport: httpx.BaseTransport | None,
    ) -> None:
        self.base_url = base_url
        self.http = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers=_headers(token, headers),
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
        expect: Expect = "json",
    ) -> Any:
        response = self.http.request(
            method, path, params=sent(query), json=sent(body), **_options(expect)
        )
        raise_for_status(response)
        return _decode(response, expect)

    def stream(
        self, path: str, *, query: Mapping[str, Any] | None = None
    ) -> Stream[Any]:
        return Stream(self.http, path, sent(query))

    def close(self) -> None:
        self.http.close()


class AsyncTransport:
    """`Transport`, awaited."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout: float | httpx.Timeout,
        headers: Mapping[str, str] | None,
        transport: httpx.AsyncBaseTransport | None,
    ) -> None:
        self.base_url = base_url
        self.http = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers=_headers(token, headers),
        )

    async def request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
        expect: Expect = "json",
    ) -> Any:
        response = await self.http.request(
            method, path, params=sent(query), json=sent(body), **_options(expect)
        )
        raise_for_status(response)
        return _decode(response, expect)

    def stream(
        self, path: str, *, query: Mapping[str, Any] | None = None
    ) -> AsyncStream[Any]:
        return AsyncStream(self.http, path, sent(query))

    async def close(self) -> None:
        await self.http.aclose()
