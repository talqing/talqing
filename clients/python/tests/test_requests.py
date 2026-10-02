"""What the client actually puts on the wire, and what it does with the answer."""

from __future__ import annotations

import json
from typing import Any, Callable

import httpx
import pytest

from talqing import AsyncTalqing, Talqing, TalqingAPIError, paginate, paginate_async

BASE = "https://api.example.com"


def build(handler: Callable[[httpx.Request], httpx.Response]) -> Talqing:
    return Talqing(
        token="secret", base_url=BASE, transport=httpx.MockTransport(handler)
    )


def build_async(handler: Callable[[httpx.Request], httpx.Response]) -> AsyncTalqing:
    return AsyncTalqing(
        token="secret", base_url=BASE, transport=httpx.MockTransport(handler)
    )


def capture(payload: Any = None, status: int = 200) -> tuple[list[httpx.Request], Any]:
    """A handler that records every request and answers with one payload."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=payload if payload is not None else {})

    return seen, handler


# -- credentials -------------------------------------------------------------


def test_a_missing_token_says_where_to_get_one() -> None:
    with pytest.raises(ValueError, match="TALQING_API_KEY"):
        Talqing(base_url=BASE)


def test_a_missing_base_url_refuses_rather_than_guessing() -> None:
    with pytest.raises(ValueError, match="base_url is required"):
        Talqing(token="secret")


def test_from_env_reads_both(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TALQING_API_KEY", "from-env")
    monkeypatch.setenv("TALQING_BASE_URL", f"{BASE}/")
    with Talqing.from_env() as client:
        assert client.base_url == BASE
        assert client.http.headers["Authorization"] == "Bearer from-env"


def test_an_explicit_argument_beats_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TALQING_API_KEY", "from-env")
    monkeypatch.setenv("TALQING_BASE_URL", BASE)
    with Talqing.from_env(token="explicit") as client:
        assert client.http.headers["Authorization"] == "Bearer explicit"


# -- request building --------------------------------------------------------


def test_a_path_parameter_is_escaped() -> None:
    seen, handler = capture({"id": "x"})
    with build(handler) as client:
        client.agents.get("a/b?c")
    # `raw_path`, not `path`: the latter is httpx's decoded view, which would
    # read the escaping back out and hide whether it happened.
    assert seen[0].url.raw_path == b"/v1/agents/a%2Fb%3Fc"


def test_a_body_is_the_keyword_arguments() -> None:
    seen, handler = capture({"id": "x"}, status=201)
    with build(handler) as client:
        client.agents.create(config={"name": "Support", "channel": "text"})
    assert json.loads(seen[0].content) == {
        "config": {"name": "Support", "channel": "text"}
    }
    assert seen[0].headers["Authorization"] == "Bearer secret"


def test_an_omitted_argument_is_not_sent() -> None:
    seen, handler = capture({"items": [], "has_more": False, "limit": 200, "offset": 0})
    with build(handler) as client:
        client.agents.list()
    assert seen[0].url.query == b""


def test_null_is_sent_and_omission_is_not() -> None:
    """The distinction the OMIT sentinel exists for: `label=None` clears the
    label, which is not the same as not passing it."""
    seen, handler = capture({"id": "number"})
    with build(handler) as client:
        client.telephony.phone_numbers.update("n1", label=None)
        client.telephony.phone_numbers.update("n1", can_outbound=False)
    assert json.loads(seen[0].content) == {"label": None}
    assert json.loads(seen[1].content) == {"can_outbound": False}


def test_a_query_parameter_is_sent_when_given() -> None:
    seen, handler = capture({"items": [], "has_more": False, "limit": 10, "offset": 0})
    with build(handler) as client:
        client.calls.list(limit=10, type="SIP_INBOUND")
    assert dict(httpx.QueryParams(seen[0].url.query.decode())) == {
        "limit": "10",
        "type": "SIP_INBOUND",
    }


def test_a_nested_resource_reaches_its_own_path() -> None:
    seen, handler = capture({"id": "b"})
    with build(handler) as client:
        client.calls.batches.pause("batch-1")
        client.telephony.phone_numbers.assign("num-1", agent_id="agent-1")
    assert seen[0].url.path == "/v1/calls/batches/batch-1/pause"
    assert seen[1].url.path == "/v1/telephony/phone-numbers/num-1/assign"
    assert json.loads(seen[1].content) == {"agent_id": "agent-1"}


# -- responses ---------------------------------------------------------------


def test_a_no_content_response_is_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    with build(handler) as client:
        assert client.calls.recording.delete("session-1") is None


def test_a_binary_response_follows_the_redirect_and_returns_the_bytes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/recording"):
            return httpx.Response(
                302, headers={"Location": "https://cdn.example.com/call.ogg"}
            )
        return httpx.Response(
            200, content=b"OggS-audio", headers={"Content-Type": "audio/ogg"}
        )

    with build(handler) as client:
        assert client.calls.recording.get("session-1") == b"OggS-audio"


def test_a_redirect_is_not_followed_anywhere_else() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302, headers={"Location": "https://elsewhere.example.com/"}
        )

    with build(handler) as client:
        with pytest.raises(TalqingAPIError) as caught:
            client.agents.get("agent-1")
    assert caught.value.status_code == 302


def test_an_error_carries_the_message_and_every_field_problem() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "detail": {
                    "message": "config is invalid",
                    "errors": ["llm: unknown model"],
                }
            },
        )

    with build(handler) as client:
        with pytest.raises(TalqingAPIError) as caught:
            client.agents.publish("agent-1")
    assert str(caught.value) == "config is invalid"
    assert caught.value.status_code == 422
    assert caught.value.errors == ["llm: unknown model"]


def test_a_failure_that_never_reached_the_api_still_raises_ours() -> None:
    """A proxy's HTML 502 has no `detail`, and must not come back as a JSON error."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>bad gateway</html>")

    with build(handler) as client:
        with pytest.raises(TalqingAPIError) as caught:
            client.agents.get("agent-1")
    assert caught.value.status_code == 502
    assert caught.value.errors == []


def test_the_browser_redirect_is_the_same_on_both_clients() -> None:
    """It is not an operation, so nothing generates it — and nothing catches the
    two clients drifting apart except this."""
    seen, handler = capture()
    sync, asynchronous = build(handler), build_async(handler)
    for client in (sync, asynchronous):
        assert not hasattr(client, "google_login_url"), (
            "sign-in is on the control API; a helper built from a regional "
            "base_url returns a 404"
        )
        assert client.oauth_start_url("google_calendar") == (
            f"{BASE}/v1/integrations/oauth/google_calendar/start"
        )
        assert client.oauth_start_url("cal.com", "int-1") == (
            f"{BASE}/v1/integrations/oauth/cal.com/start?integration_id=int-1"
        )
    sync.close()


def test_the_escape_hatch_carries_the_credentials_and_the_error_contract() -> None:
    seen, handler = capture({"status": "ok"})
    with build(handler) as client:
        assert client.request("GET", "/v1/health") == {"status": "ok"}
    assert seen[0].headers["Authorization"] == "Bearer secret"
    assert client.http.base_url == BASE


# -- pagination --------------------------------------------------------------


def page(
    items: list[Any], *, has_more: bool, limit: int, offset: int
) -> dict[str, Any]:
    return {"items": items, "has_more": has_more, "limit": limit, "offset": offset}


def test_paginate_walks_until_the_last_page() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        offset = int(httpx.QueryParams(request.url.query.decode())["offset"])
        return httpx.Response(
            200,
            json=page([{"id": offset}], has_more=offset < 4, limit=2, offset=offset),
        )

    with build(handler) as client:
        assert [a["id"] for a in paginate(client.agents.list, limit=2)] == [0, 2, 4]
    assert len(seen) == 3


def test_paginate_passes_the_endpoint_its_own_filters() -> None:
    seen, handler = capture(page([], has_more=False, limit=200, offset=0))
    with build(handler) as client:
        assert list(paginate(client.calls.list, agent_id="agent-1")) == []
    assert dict(httpx.QueryParams(seen[0].url.query.decode())) == {
        "agent_id": "agent-1",
        "limit": "200",
        "offset": "0",
    }


# -- streams -----------------------------------------------------------------

FRAMES = (
    b": keep-alive\n\n"
    b'event: snapshot\ndata: {"event": "snapshot", "messages": []}\n\n'
    b'event: turn\ndata: {"event": "turn",\ndata: "status": "done"}\n\n'
)


def test_a_stream_yields_one_decoded_frame_at_a_time() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Accept"] == "text/event-stream"
        return httpx.Response(
            200, content=FRAMES, headers={"Content-Type": "text/event-stream"}
        )

    with build(handler) as client:
        events = list(client.copilot.agents.stream("agent-1"))
    assert events == [
        {"event": "snapshot", "messages": []},
        {"event": "turn", "status": "done"},
    ]


def test_a_stream_can_be_left_early() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=FRAMES, headers={"Content-Type": "text/event-stream"}
        )

    with build(handler) as client:
        with client.conversations.events("conversation-1") as events:
            for event in events:
                assert event["event"] == "snapshot"
                break


def test_a_stream_that_fails_raises_before_the_first_frame() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404, json={"detail": {"message": "no such agent", "errors": []}}
        )

    with build(handler) as client:
        with pytest.raises(TalqingAPIError, match="no such agent"):
            list(client.copilot.agents.stream("agent-1"))


def test_a_stream_is_single_use() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=FRAMES, headers={"Content-Type": "text/event-stream"}
        )

    with build(handler) as client:
        stream = client.conversations.events("conversation-1")
        assert len(list(stream)) == 2
        with pytest.raises(RuntimeError, match="already|finished"):
            list(stream)


# -- the async client behaves the same ---------------------------------------


async def test_async_sends_the_same_request() -> None:
    seen, handler = capture({"id": "x"}, status=201)
    async with build_async(handler) as client:
        await client.agents.create(config={"name": "Support", "channel": "text"})
    assert json.loads(seen[0].content) == {
        "config": {"name": "Support", "channel": "text"}
    }


async def test_async_streams_without_an_await_in_front() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=FRAMES, headers={"Content-Type": "text/event-stream"}
        )

    async with build_async(handler) as client:
        events = [event async for event in client.copilot.tools.stream("tool-1")]
    assert [event["event"] for event in events] == ["snapshot", "turn"]


async def test_async_paginates() -> None:
    seen, handler = capture(page([{"id": 1}], has_more=False, limit=200, offset=0))
    async with build_async(handler) as client:
        assert [a async for a in paginate_async(client.agents.list)] == [{"id": 1}]
    assert len(seen) == 1


async def test_async_raises_the_same_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, json={"detail": {"message": "viewers may not", "errors": []}}
        )

    async with build_async(handler) as client:
        with pytest.raises(TalqingAPIError) as caught:
            await client.agents.delete("agent-1")
    assert caught.value.status_code == 403
