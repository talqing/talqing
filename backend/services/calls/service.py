"""Calls business ops: LiveKit token mint + list/inspect voice/video sessions."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import secrets as pysecrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

import asyncpg
from fastapi import HTTPException
from livekit.api import AccessToken, RoomAgentDispatch, RoomConfiguration, VideoGrants
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from api.core.schemas import ErrorBody, Page
from services import close_reasons, credits, jobs, recordings, session_events
from services.agents import AgentConfig
from services.agents.plan import (
    AgentPlanRequest,
    StoredAgentPlan,
    draft_agent_ids,
    load_plan_roster,
    resolve_call_plan,
)
from services.analysis import AnalysisResponse, Outcome
from services.conversations import ConversationItemResponse, SessionError, ensure_web_ref
from services.conversations.service import items_out
from services.recordings import RecordingState, RecordingStatus
from services.session_snapshot import HealthSnapshotResponse, RunFacts, build_health_snapshot
from services.sessions.detail import load_session_reads
from services.sessions.models import (
    AgentMetadata,
    CostDetailResponse,
    CostResponse,
    SessionEventResponse,
    analysis_from_row,
    detail_cost,
    summary_cost,
)
from services.user import Context
from services.userdata import RESERVED_KEY_ERROR, is_reserved_key
from settings import get_settings

logger = logging.getLogger("talqing.services.calls")

# ─────────────────────────────── response models ────────────────────────────


class CallTokenResponse(BaseModel):
    """How to join this call, and which call it is."""

    # Response-only: `warnings` is always sent, so its default must not publish
    # it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    # Nothing else, by rule: at mint time the call has not happened yet, so
    # there is no call resource to return. Anything derivable from `session_id`
    # is one `GET /v1/calls/{id}` away — which deliberately does not 404 on the
    # id returned here (see the comment on the INSERT below). `warnings` is the
    # exception, and it is here because it exists nowhere else: nothing writes
    # it to the session row.
    server_url: str
    participant_token: str
    # This call's id — the same one `GET /v1/calls/{id}` answers to.
    session_id: UUID
    # Everything the plan resolved but could not refuse — an unreachable team
    # member, a handoff into an agent that records when this one does not.
    # Resolved against state at CALL time, so this is not the same list
    # `publish_agent` returned, even though it is the same shape.
    warnings: list[str] = Field(default_factory=list)


class TokenRequest(AgentPlanRequest):
    """One browser call: who is on it, what it starts knowing, and what runs.

    `vars` comes from `AgentPlanRequest` and lasts exactly as long as this call.
    """

    # Your own stable id for the person calling. Omit it and we mint a
    # single-use one, so the call has no history to continue.
    contact_key: str | None = None
    userdata: dict[str, object] | None = None


class CallPlanSummary(BaseModel):
    """What was different about this call, in the facts a list row can show.

    The plan itself is not served here — a page of 200 calls does not want 200
    configs — and these are enough to say `Inline`, `Draft`, `Overridden`,
    `Pinned` or `Team · 3` without opening anything.
    """

    # How many agents the call could run. 1 unless it was a team.
    members: int
    # Whether the agent that answered was defined in the request that started the
    # call, and so has no row to link to.
    inline: bool
    # Whether the agent that answered ran its unpublished draft.
    draft: bool
    # Whether anything was layered on the version it ran. False when the request
    # only pinned a version — which is a different thing, and saying "overridden"
    # for it would be a claim about the config that is not true.
    overridden: bool


# How a call reached the agent. A closed set, and named so every generated SDK
# gets the name rather than a bare `str` each client re-declares for itself.
# `STREAM` is one socket from a partner's contact-centre platform: one type, not
# an inbound/outbound pair, because the partner owns the dialling and the
# direction is something we are told or do not know, never something we did.
# `WHATSAPP_INBOUND` is a user tapping "call" in their WhatsApp chat with one of
# the workspace's numbers.
CallType = Literal["WEB", "SIP_INBOUND", "SIP_OUTBOUND", "STREAM", "WHATSAPP_INBOUND"]


class CallResponseBase(BaseModel):
    id: UUID
    agent: AgentMetadata
    type: CallType
    status: str
    close_reason: str | None = None
    # The same ending in a sentence, from `services.close_reasons`. Served
    # rather than mapped client-side because `close_reason` has around forty
    # values written from five places — including a whole `sip_<carrier
    # reason>_failed` family generated at runtime — and any second copy of that
    # list goes stale the first time a worker learns a new one.
    close_reason_label: str | None = None
    started_at: datetime
    ended_at: datetime | None = None
    # SIP legs (null for web / text).
    from_e164: str | None = None
    to_e164: str | None = None
    conversation_id: UUID | None = None


class RecordingResponse(BaseModel):
    """The call's audio, as far as a reader is concerned.

    ``state`` — not the stored status — is what decides whether there is
    anything to play; ``services.recordings.resolve_state`` answers it in one
    place for every caller.

    The two links are minted on the **call detail** only, and only while the
    state is ``available``. A list of 200 calls would otherwise carry 200 signed
    URLs nobody asked for, and each of them dies in an hour. ``expires_at`` is
    detail-only for the same reason — it costs a lookup per call — so it reads
    ``null`` in a list whether or not a retention policy is set. ``state`` and
    the sizes are on both.
    """

    state: RecordingState
    started_at: datetime | None = None
    # Container duration, so it matches what the player's scrub bar will show.
    duration_s: int | None = None
    bytes: int | None = None
    # A direct link to the audio in object storage, good for `Range` requests —
    # which is how a player seeks without downloading the whole file first.
    url: str | None = None
    # The same object, signed so the bucket serves it as a download named
    # `call-{id}.ogg`. Separate because one URL cannot be both: the disposition
    # is signed into it, and an `<a download>` cannot rename a cross-origin file.
    download_url: str | None = None
    # When both links stop working. The recording itself is untouched — ask
    # again for fresh ones.
    url_expires_at: datetime | None = None
    # When this call's content is scheduled to be deleted under the
    # organization's retention policy. Null when retention is unlimited, which
    # is the default.
    expires_at: datetime | None = None


def _recording_from_row(row: Mapping[str, object]) -> RecordingResponse:
    started_at = row["recording_started_at"]
    assert started_at is None or isinstance(started_at, datetime)
    ended_at = row["ended_at"]
    assert ended_at is None or isinstance(ended_at, datetime)
    state = recordings.resolve_state(
        str(row["recording_status"]), started_at, session_ended_at=ended_at
    )
    return RecordingResponse(
        state=state,
        started_at=started_at,
        duration_s=row["recording_duration_s"],  # type: ignore[arg-type]
        bytes=row["recording_bytes"],  # type: ignore[arg-type]
    )


def _screenshare_from_row(row: Mapping[str, object]) -> RecordingResponse:
    """The screen video, through the same playability rule as the audio.

    `resolve_state` is deliberately shared: "the call detail said it was there"
    and "the player 404s" must not be able to disagree for either artifact, and
    a second copy of that arithmetic is how they start to.
    """
    started_at = row["screenshare_recording_started_at"]
    assert started_at is None or isinstance(started_at, datetime)
    ended_at = row["ended_at"]
    assert ended_at is None or isinstance(ended_at, datetime)
    return RecordingResponse(
        state=recordings.resolve_state(
            str(row["screenshare_recording_status"]), started_at, session_ended_at=ended_at
        ),
        started_at=started_at,
        duration_s=row["screenshare_recording_duration_s"],  # type: ignore[arg-type]
        bytes=row["screenshare_recording_bytes"],  # type: ignore[arg-type]
    )


def _stream_from(row: Mapping[str, object], events: Sequence[Mapping[str, object]]):
    """The partner-stream block, from the session row and the call's own trace.

    The codec and the rate are on `stream.connected` rather than on a column,
    for the reason every negotiated fact is: they are announced per call by the
    platform, so a column would be a second copy that could only ever disagree
    with the wire.
    """
    if row["type"] != "STREAM":
        return None
    payload: Mapping[str, object] = {}
    for event in events:
        if event["type"] == session_events.STREAM_CONNECTED:
            candidate = event["payload"]
            payload = candidate if isinstance(candidate, dict) else {}
            break
    return StreamResponse(
        connection_id=row["stream_connection_id"],  # type: ignore[arg-type]
        connection_name=row["stream_connection_name"],  # type: ignore[arg-type]
        dialect=row["stream_dialect"] or payload.get("dialect"),  # type: ignore[arg-type]
        platform_call_id=row["idempotency_key"],  # type: ignore[arg-type]
        codec=payload.get("codec"),  # type: ignore[arg-type]
        sample_rate=payload.get("sample_rate"),  # type: ignore[arg-type]
    )


class CallSummaryResponse(CallResponseBase):
    cost: CostResponse | None = None
    # Null on an ordinary call — one agent, its published version — which is
    # what almost every row is.
    plan: CallPlanSummary | None = None
    # Who the call was with. Stable across every call from the same person, and
    # the key `list_calls(contact_key=...)` filters on.
    contact_key: str | None = None
    # Which partner media stream carried this call. Null on every other type.
    # Carried on the LIST row rather than derived from `contact_key`: the key IS
    # `stream:{connection_id}:{peer}` by construction, but parsing that format
    # client-side would be a second copy of it, in another language, that
    # nothing forces to agree with `stream_conversation_key`.
    stream_connection_id: UUID | None = None
    # Wall-clock seconds; null while the call is still running.
    duration_s: int | None = None
    recording: RecordingResponse
    # Enough to render a list row without opening the call.
    message_count: int = 0
    # Provider-reported caller + agent speech, in milliseconds.
    total_audio_ms: int = 0
    # The two analysis outputs a list row shows. The rationale and the extracted
    # fields need the whole call around them, so they wait for the detail view.
    summary: str | None = None
    # The same three-valued vocabulary the detail view's `analysis.outcome`
    # uses. It was a bare `str` here, which published a narrower guarantee than
    # the code makes to a reader of the list and a wider one than the analysis
    # ever produces.
    outcome: Outcome | None = None


class TransferResponse(BaseModel):
    """How this call was handed to a human, when it was.

    Sits beside ``close_reason`` because it *is* the ending: a transferred call
    is `completed` and short, and without this the short duration and the
    truncated recording read as a call that failed early rather than as one that
    reached a person.

    ``detail`` is the plain-English reason the agent was given, and through it
    the caller — "nobody answered", "they aren't able to take the call right
    now". Null on a transfer that connected. ``sip_status`` is the raw carrier
    verdict (``{"code": 486, "phrase": "Busy Here"}``) and is null unless the
    carrier gave one; it is for support, and the two are different strings on
    purpose (a model handed "486" will eventually read it aloud).

    ``transport`` is ``bridge`` for every warm transfer: no SIP REFER shape lets
    the agent speak to the person answering first. ``mode`` is what carries the
    warm/cold distinction.
    """

    mode: Literal["cold", "warm"]
    transport: Literal["refer", "bridge"]
    destination: str
    outcome: Literal["connected", "failed"]
    detail: str | None = None
    sip_status: dict[str, object] | None = None
    # What the tool did after a transfer that did not connect: `continue` gives
    # the caller back to the agent, `end_call` hangs up on them. Null on a
    # transfer that connected, and on records written before this was stored.
    on_failure: Literal["continue", "end_call"] | None = None
    at: datetime


class StreamResponse(BaseModel):
    """Which partner connection carried this call, and on what.

    Sits beside `transfer` for the same reason it does: it explains something
    about the call that no other field can. A `STREAM` call has no phone number
    and no carrier account, so without this the "Runtime identifiers" panel is a
    row of nulls and there is nothing to say where the audio came from.
    """

    connection_id: UUID | None = None
    connection_name: str | None = None
    dialect: str | None = None
    # The partner's own id for the call, which is what a support ticket quotes.
    platform_call_id: str | None = None
    codec: str | None = None
    sample_rate: int | None = None


class CallSessionResponse(CallResponseBase):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    # the call's final userdata (the variable bus) — what it collected/computed
    userdata: dict[str, object] | None = None
    duration_s: int | None = None
    recording: RecordingResponse
    # The screen the person shared, if the agent watched one and the author
    # asked for it to be kept: a second file of the same call, 1 fps, in the same
    # shape as the audio above. `state` is `none` on every call where screen
    # recording was off and `not_shared` where it was on and nobody shared —
    # only one of those is a setting somebody would want to change.
    screen_recording: RecordingResponse
    # Null on every call that never attempted a transfer.
    transfer: TransferResponse | None = None
    # Null on every call that did not arrive over a partner's media stream.
    stream: StreamResponse | None = None
    # Who the call was with — stable across every call from the same person,
    # while `conversation_id` is new on each one for an agent that starts clean.
    # The `session.completed` webhook has always carried it; without it here the
    # dashboard is the one surface that cannot group a customer's calls.
    contact_key: str | None = None
    # What the platform knows about this caller across ALL their calls, as
    # opposed to `userdata` above, which is what THIS call ended holding.
    contact_userdata: dict[str, object] | None = None
    # Correlation ids for support and log-grepping — the "Runtime identifiers"
    # panel, not something a caller normally reads.
    livekit_room: str | None = None
    conversation_ref_id: UUID | None = None
    trigger_id: UUID | None = None
    integration_id: UUID | None = None
    phone_number_id: UUID | None = None
    telephony_account_id: UUID | None = None
    # "draft" when the call ran the agent's unpublished draft.
    agent_version: int | Literal["draft"] | None = None
    billing_status: str = "pending"
    billed_at: datetime | None = None
    # What post-call analysis worked out, in the same shape the `session.completed`
    # webhook carries — one definition, so an integration that reads one reads
    # the other. Note `userdata` above and `analysis.fields` here are separate
    # on purpose: userdata is what the agent knew, this is what a reader
    # inferred afterwards.
    analysis: AnalysisResponse = Field(default_factory=AnalysisResponse)
    # Set when the run died on an unhandled error, not on a normal close. The
    # same typed model the conversations API serves off the same column — one
    # shape, so the two surfaces cannot render the same failure differently.
    error: SessionError | None = None
    # The deletion receipt: when this call's content was erased, by the
    # organization's retention policy or by `delete_call`. Null on every call
    # that still has its content. It is what tells an empty transcript apart
    # from a call where nobody spoke — the same rows, two opposite meanings.
    content_deleted_at: datetime | None = None


class LLMUsageResponse(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    provider: str
    model: str
    input_tokens: int = 0
    input_cached_tokens: int = 0
    output_tokens: int = 0
    # 'conversation' (the call itself) or 'analysis' (the one call that read the
    # finished transcript). Both price identically; kept apart so a tenant can
    # see what enabling analysis costs them.
    purpose: str = "conversation"
    # Whether the agent asked for the provider's priority lane on this model,
    # which prices at a different rate block — up to twice the standard one. It
    # is the reason an LLM line can cost double what the catalog suggests.
    priority: bool = False


class TTSUsageResponse(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    provider: str
    model: str
    characters_count: int = 0
    audio_duration: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0


class STTUsageResponse(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    provider: str
    model: str
    audio_duration: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0


class RealtimeUsageResponse(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    provider: str
    model: str
    input_text_tokens: int = 0
    input_cached_text_tokens: int = 0
    input_audio_tokens: int = 0
    input_cached_audio_tokens: int = 0
    output_text_tokens: int = 0
    output_audio_tokens: int = 0
    session_seconds: float = 0.0


class AvatarUsageResponse(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    provider: str
    model: str
    seconds: float = 0.0
    avatar_id: str | None = None
    avatar_session_id: str | None = None


class UsageResponse(BaseModel):
    """A call fills either llm+tts+stt or realtime — never both, because the
    agent ran one pipeline or the other."""

    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    llm: list[LLMUsageResponse]
    tts: list[TTSUsageResponse]
    stt: list[STTUsageResponse]
    realtime: list[RealtimeUsageResponse]
    avatar: list[AvatarUsageResponse]


# No per-call latency block here on purpose. The same averages existed in three
# places — this response, the snapshot, and `sessions.metrics` — and this
# was the copy nothing read: every surface recomputes from `snapshot`. One home,
# and it is the snapshot, which also carries the distribution a mean cannot.


class CallToolResponse(BaseModel):
    """A tool the agent could call, as it stood on this call.

    ``id`` is null for a tool defined inline in the request that started the
    call: it has no `tools` row, and inventing an id would offer a link to a
    page that does not exist.
    """

    id: str | None = None
    name: str
    description: str
    json_schema: dict[str, object] = Field(default_factory=dict)
    long_running_task: bool = False
    silent: bool = False
    disable_interruptions: bool = False


class CallTeamMemberResponse(BaseModel):
    """One agent on the call's roster. `[0]` is the one that answered."""

    name: str
    # Null for a member defined inline in the request that started the call.
    agent_id: UUID | None = None
    # "draft" when this member ran its agent's unpublished draft.
    version: int | Literal["draft"] | None = None


class CallDetailResponse(BaseModel):
    session: CallSessionResponse
    snapshot: HealthSnapshotResponse
    transcript: list[ConversationItemResponse]
    usage: UsageResponse
    cost: CostDetailResponse | None = None
    events: list[SessionEventResponse]
    # Everything below is what this call actually ran — the pinned version, plus
    # whatever the request that started it overrode or defined outright — not
    # what the agent looks like now.
    tools: list[CallToolResponse]
    starting_prompt: str = ""
    greeting: str | None = None
    # The resolved entry config, served always rather than only when it was
    # overridden, so no client ever has to merge one itself.
    agent_config: AgentConfig | None = None
    # What was ASKED for, when it differed from "the entry agent's published
    # version". Null on an ordinary call. Diff it against the published version
    # to see what this one call did differently.
    agent_plan: StoredAgentPlan | None = None
    # The `{{vars.*}}` values the request that started this call supplied —
    # the SESSION bag, not the merged one. The declared defaults are already
    # visible through `agent_config.vars`, so this is not a second copy that
    # could drift. Detail only, never the list: a page of 200 calls does not
    # want 200 bags.
    vars: dict[str, str] = Field(default_factory=dict)
    # The cast, `[0]` being the agent that answered. One entry on an ordinary
    # call, so a reader has one shape rather than two.
    team: list[CallTeamMemberResponse] = Field(default_factory=list)


class CallStatsResponse(BaseModel):
    """Range-wide totals for the calls page summary cards.

    ``total_calls`` counts every matching voice/video session. Spend and
    ``avg_cost_per_call`` use only priced sessions (``billing_status =
    'computed'``) so pending/unpriceable rows do not understate the average.
    """

    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    total_calls: int
    priced_calls: int
    total_minutes: float
    total_spend: float
    avg_cost_per_call: float
    # Success rate is over calls post-call analysis actually judged. A call it
    # skipped, or one from before an outcome was defined, is not a failure —
    # counting those would make the rate fall every time a wrong number came in.
    judged_calls: int = 0
    successful_calls: int = 0
    start: datetime
    end: datetime


# ─────────────────────────────────── routes ─────────────────────────────────


def _api_status(status: str) -> str:
    # DB stores running; expose in_progress for existing CallSummary consumers.
    return "in_progress" if status == "running" else status


# The tenant is taken from the caller's session/PAT, never from an input.
# `contact_key` is the tenant's stable id for the person calling; omit it and we
# mint a random one for this call.
def _validate_initial_userdata(userdata: object) -> dict[str, object]:
    if userdata is None:
        return {}
    if not isinstance(userdata, dict):
        raise HTTPException(status_code=400, detail="userdata must be a JSON object")
    out: dict[str, object] = {}
    for key, value in userdata.items():
        if not isinstance(key, str) or not key.strip():
            raise HTTPException(status_code=400, detail="userdata keys must be non-empty strings")
        if is_reserved_key(key):
            raise HTTPException(status_code=400, detail=RESERVED_KEY_ERROR)
        out[key] = value
    return out


async def calls_token(
    body: TokenRequest,
    ctx: Context,
) -> CallTokenResponse:
    """Mint a browser token for a voice or video call.

    Two bags, and the difference matters. `userdata` is about the PERSON: it is
    merged into their contact record, so it is there again on their next call and
    any tool may overwrite it mid-call. `vars` is about THIS session: read-only,
    gone when the call ends, and never written onto anybody's record — which is
    what makes it the place for deployment configuration such as which API host
    this reseller's agents should call. Its values are substituted into the
    prompt the model reads, so they are not a place for credentials; put those in
    a workspace secret and read `{{secrets.NAME}}` from a tool.
    """
    plan = await resolve_call_plan(
        ctx,
        body,
        channels=("voice", "video"),
        channel_hint=(
            " - text agents cannot join LiveKit rooms; use POST /v1/conversations/messages instead"
        ),
    )
    entry = plan.entry
    cfg = entry.config

    # After `resolve_call_plan`, before the session row: an unpublished agent is
    # still an unpublished agent when you are out of credits, and that is the more
    # useful error — but a token we never mint must not leave a `queued` row
    # behind. `calls_token` only resolves voice and video, so every call through
    # here is billable.
    if not await credits.has_credit(ctx.tenant):
        raise HTTPException(
            status_code=402,
            detail=ErrorBody(message=credits.INSUFFICIENT_CREDITS_MESSAGE, errors=[]).model_dump(),
        )

    s = get_settings()
    session_id = uuid4()
    contact_key = (body.contact_key or "").strip() or f"web-call:{pysecrets.token_hex(8)}"
    initial_userdata = _validate_initial_userdata(body.userdata)
    # Session starts with the entry agent. Handoffs stay in-call.
    thread = await ensure_web_ref(
        ctx,
        conversation_key=contact_key,
        source="web",
        context=cfg.conversation.context,
        userdata_seed=initial_userdata or None,
    )

    # The row is written HERE, not when the worker starts, and that is what gives
    # the plan somewhere to live: the LiveKit RoomConfiguration metadata is
    # embedded in the JWT the browser receives, which is the wrong place for a
    # multi-KB plan. `create_session` is an ON CONFLICT (id) upsert whose status
    # rule promotes queued -> running, so the worker path needs no change at all.
    #
    # Two things fall out, and both are improvements: `GET /v1/calls/{id}` no
    # longer 404s on the id this call just returned, and a token minted and never
    # joined is visible as a queued row rather than as nothing.
    pool = await ctx.tenant_pool()
    await pool.execute(
        """
        INSERT INTO sessions (
            id, tenant_id, conversation_id, agent_id, agent_version_id,
            agent_name, channel, type, status, conversation_ref_id, agent_plan,
            vars
        )
        VALUES ($1, $2, $3, $4::uuid, $5::uuid, $6, $7, 'WEB', 'queued', $8, $9::jsonb,
                $10::jsonb)
        """,
        session_id,
        ctx.tenant.id,
        thread.conversation_id,
        entry.agent_id,
        entry.agent_version_id,
        cfg.name,
        cfg.channel,
        thread.ref.id,
        json.dumps(plan.stored()) if plan.stored() is not None else None,
        json.dumps(plan.vars) if plan.vars else None,
    )

    room = str(session_id)
    identity = f"user-{pysecrets.token_hex(4)}"
    # Chosen once per call: the browser connects here, and the worker hands the
    # same URL to the Anam engine so both reach this room through one SFU.
    urls = s.livekit.public_urls
    server_url = urls[random.randrange(len(urls))]
    metadata = json.dumps(
        {
            "tenant": str(ctx.tenant.id),
            # Null on an inline entry agent, which genuinely has no agent row.
            # The worker reads the plan off the session for those.
            "agent": entry.agent_id,
            "version": entry.version,
            "channel": cfg.channel,
            "session": str(session_id),
            "conversation_id": str(thread.conversation_id),
            "conversation_ref_id": str(thread.ref.id),
            "contact_key": thread.ref.conversation_key,
            "participant_identity": identity,
            "server_url": server_url,
            "userdata": initial_userdata,
        }
    )
    token = (
        AccessToken(s.livekit.api_key, s.livekit.api_secret)
        .with_identity(identity)
        .with_grants(VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True))
        .with_room_config(
            RoomConfiguration(
                metadata=metadata,
                agents=[RoomAgentDispatch(agent_name=s.livekit.agent_name, metadata=metadata)],
            )
        )
        .to_jwt()
    )
    return CallTokenResponse(
        server_url=server_url,
        participant_token=token,
        session_id=session_id,
        warnings=list(plan.warnings),
    )


# Default observability window: the trailing 30 days. Resolved here so a
# missing `start`/`end` query param means "last 30 days" consistently across the
# list and the stats aggregate.
def _resolve_window(start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    end = end or datetime.now(UTC)
    start = start or (end - timedelta(days=30))
    return start, end


async def list_calls(
    ctx: Context,
    limit: int = 200,
    offset: int = 0,
    start: datetime | None = None,
    end: datetime | None = None,
    agent_id: UUID | None = None,
    type_: str | None = None,
    status: str | None = None,
    outcome: str | None = None,
    contact_key: str | None = None,
    close_reason: str | None = None,
    batch_id: UUID | None = None,
) -> Page[CallSummaryResponse]:
    start, end = _resolve_window(start, end)
    pool = await ctx.tenant_pool()
    # fetch one extra row to learn there's a next page without a count(*).
    # Missing agent_id means ALL agents; otherwise limit to that one agent.
    #
    # `contact_key` is the only filter that needs the identity join, and it is
    # what makes "every call with this person" askable: `conversation_id` is new
    # on each call for an agent that starts clean, and a phone number is one of
    # several a person may ring from.
    sessions = await pool.fetch(
        "SELECT s.id, s.agent_id, s.agent_name, s.channel, s.type, s.status, s.close_reason, "
        "COALESCE(s.started_at, s.created_at) AS started_at, s.ended_at, s.conversation_id, "
        "s.from_e164, s.to_e164, "
        "s.billing_status, s.provider_cost, s.platform_fee, s.total_charge, s.duration_s, "
        "s.stream_connection_id, "
        "s.recording_status, s.recording_started_at, s.recording_duration_s, s.recording_bytes, "
        "s.summary, s.outcome, ref.conversation_key AS contact_key, "
        # Two facts off the plan rather than the plan itself: a page of 200 calls
        # does not want 200 configs, and these are what a badge needs.
        "jsonb_array_length(s.agent_plan -> 'members') AS plan_members, "
        "(s.agent_plan -> 'members' -> 0 ->> 'agent_id') IS NULL AS plan_inline, "
        # A stored member names an agent and no version only when it ran the
        # draft (`draft_agent_ids`); its override is then the whole draft rather
        # than changes layered on a version, so it is not also "overridden".
        "((s.agent_plan -> 'members' -> 0 ->> 'agent_id') IS NOT NULL "
        " AND (s.agent_plan -> 'members' -> 0 ->> 'version') IS NULL) AS plan_draft, "
        "(s.agent_plan -> 'members' -> 0 -> 'override') <> '{}'::jsonb AS plan_overridden "
        "FROM sessions s "
        "LEFT JOIN conversations c "
        "  ON c.id = s.conversation_id AND c.tenant_id = s.tenant_id "
        "LEFT JOIN conversation_refs ref "
        "  ON ref.id = c.conversation_ref_id AND ref.tenant_id = c.tenant_id "
        "WHERE s.tenant_id = $3 AND s.channel IN ('voice', 'video') "
        "AND COALESCE(s.started_at, s.created_at) >= $4 "
        "AND COALESCE(s.started_at, s.created_at) <= $5 "
        "AND ($6::uuid IS NULL OR s.agent_id = $6::uuid) "
        "AND ($7::text IS NULL OR s.type = $7::text) "
        "AND ($8::text IS NULL OR s.status = $8::text) "
        "AND ($9::text IS NULL OR s.outcome = $9::text) "
        "AND ($10::text IS NULL OR ref.conversation_key = $10::text) "
        "AND ($11::text IS NULL OR s.close_reason = $11::text) "
        # Served by `idx_sessions_batch` off the column the dispatcher writes at
        # claim time — no join against up to 10 000 recipient ids, and superseded
        # retry attempts stay findable.
        "AND ($12::uuid IS NULL OR s.batch_id = $12::uuid) "
        "ORDER BY COALESCE(s.started_at, s.created_at) DESC LIMIT $1 OFFSET $2",
        limit + 1,
        offset,
        ctx.tenant.id,
        start,
        end,
        agent_id,
        type_,
        # The API exposes `in_progress`; the column stores `running`.
        "running" if status == "in_progress" else status,
        outcome,
        contact_key,
        close_reason,
        batch_id,
    )
    has_more = len(sessions) > limit
    sessions = sessions[:limit]
    session_ids = [row["id"] for row in sessions]

    # Two rollups for the whole page, never one query per row.
    counts, audio = await asyncio.gather(
        pool.fetch(
            "SELECT session_id, COUNT(*) AS messages FROM conversation_items "
            "WHERE tenant_id = $1 AND session_id = ANY($2::uuid[]) AND type = 'message' "
            "GROUP BY session_id",
            ctx.tenant.id,
            session_ids,
        ),
        pool.fetch(
            "SELECT session_id, SUM(audio_duration) AS seconds FROM ("
            "  SELECT session_id, audio_duration FROM stt_usage "
            "  WHERE tenant_id = $1 AND session_id = ANY($2::uuid[]) "
            "  UNION ALL "
            "  SELECT session_id, audio_duration FROM tts_usage "
            "  WHERE tenant_id = $1 AND session_id = ANY($2::uuid[])"
            ") AS speech GROUP BY session_id",
            ctx.tenant.id,
            session_ids,
        ),
    )
    messages_by_session = {row["session_id"]: int(row["messages"]) for row in counts}
    audio_ms_by_session = {row["session_id"]: round(float(row["seconds"]) * 1000) for row in audio}

    items = [
        CallSummaryResponse(
            id=row["id"],
            agent=AgentMetadata(
                agent_id=row["agent_id"],
                name=row["agent_name"],
                channel=row["channel"],
            ),
            type=row["type"],
            status=_api_status(row["status"]),
            close_reason=row["close_reason"],
            close_reason_label=close_reasons.describe(row["close_reason"])
            if row["close_reason"]
            else None,
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            from_e164=row["from_e164"],
            to_e164=row["to_e164"],
            conversation_id=row["conversation_id"],
            contact_key=row["contact_key"],
            stream_connection_id=row["stream_connection_id"],
            duration_s=row["duration_s"],
            message_count=messages_by_session.get(row["id"], 0),
            total_audio_ms=audio_ms_by_session.get(row["id"], 0),
            cost=summary_cost(row),
            recording=_recording_from_row(row),
            summary=row["summary"],
            outcome=row["outcome"],
            plan=(
                CallPlanSummary(
                    members=row["plan_members"],
                    inline=row["plan_inline"],
                    draft=row["plan_draft"],
                    overridden=row["plan_overridden"] and not row["plan_draft"],
                )
                if row["plan_members"] is not None
                else None
            ),
        )
        for row in sessions
    ]
    return Page[CallSummaryResponse](items=items, has_more=has_more, limit=limit, offset=offset)


# Declared BEFORE /{session_id} so the literal path isn't captured as a UUID.
async def call_stats(
    ctx: Context,
    start: datetime | None = None,
    end: datetime | None = None,
    agent_id: UUID | None = None,
) -> CallStatsResponse:
    """Range-wide totals for the calls summary cards."""
    start, end = _resolve_window(start, end)
    pool = await ctx.tenant_pool()
    agg = await pool.fetchrow(
        """
        SELECT
            COUNT(*) AS calls,
            COUNT(*) FILTER (
                WHERE billing_status = 'computed' AND total_charge IS NOT NULL
            ) AS priced_calls,
            COALESCE(
                SUM(total_charge) FILTER (
                    WHERE billing_status = 'computed' AND total_charge IS NOT NULL
                ),
                0
            ) AS spend,
            COALESCE(SUM(duration_s), 0) AS secs,
            -- Success rate is over JUDGED calls, not all calls: a call analysis
            -- skipped or never judged is not a failure, and counting it as one
            -- would make the rate drop every time a wrong number came in.
            COUNT(*) FILTER (WHERE outcome IS NOT NULL) AS judged_calls,
            COUNT(*) FILTER (WHERE outcome = 'success') AS successful_calls
        FROM sessions
        WHERE tenant_id = $1
            AND channel IN ('voice', 'video')
            AND COALESCE(started_at, created_at) >= $2
            AND COALESCE(started_at, created_at) <= $3
            AND ($4::uuid IS NULL OR agent_id = $4::uuid)
        """,
        ctx.tenant.id,
        start,
        end,
        agent_id,
    )
    total_calls = int(agg["calls"])
    priced_calls = int(agg["priced_calls"])
    total_spend = float(agg["spend"])
    total_seconds = float(agg["secs"])
    judged_calls = int(agg["judged_calls"])
    successful_calls = int(agg["successful_calls"])
    return CallStatsResponse(
        total_calls=total_calls,
        priced_calls=priced_calls,
        judged_calls=judged_calls,
        successful_calls=successful_calls,
        total_minutes=round(total_seconds / 60.0, 2),
        total_spend=total_spend,
        avg_cost_per_call=round(total_spend / priced_calls, 6) if priced_calls else 0.0,
        start=start,
        end=end,
    )


async def _config_tools(
    pool: asyncpg.Pool, tenant_id: UUID, config: AgentConfig | None
) -> list[CallToolResponse]:
    """The tools this call could actually call.

    `config.tools` pins each attached tool to the version that ran; the lifecycle
    hooks are pinned the same way but never reached the model, so they are not
    listed here. A tool defined inline in the request IS its definition and
    needs no lookup.

    A stored tool deleted since the call took its versions with it and simply
    drops out — a call that happened is still worth reading for everything else
    it holds.
    """
    if config is None:
        return []
    pinned = [(sel.tool_id, sel.tool_version) for sel in config.tools if sel.tool_id]
    by_id: dict[str, dict[str, object]] = {}
    if pinned:
        rows = await pool.fetch(
            """
            SELECT tv.tool_id, tv.definition
            FROM unnest($2::uuid[], $3::int[]) AS p(tool_id, version)
            JOIN tool_versions tv
                ON tv.tool_id = p.tool_id AND tv.version = p.version AND tv.tenant_id = $1
            """,
            tenant_id,
            [tool_id for tool_id, _ in pinned],
            [version for _, version in pinned],
        )
        by_id = {str(r["tool_id"]): r["definition"] for r in rows}
    out: list[CallToolResponse] = []
    for sel in config.tools:
        if sel.tool is not None:
            out.append(CallToolResponse.model_validate(sel.tool.model_dump(mode="json")))
        elif sel.tool_id in by_id:
            out.append(CallToolResponse.model_validate(by_id[sel.tool_id]))
    return out


async def get_call(session_id: UUID, ctx: Context) -> CallDetailResponse:
    """`session_id` path param is the session id (kept for API stability)."""
    pool = await ctx.tenant_pool()
    session = await pool.fetchrow(
        "SELECT s.id, s.agent_id, s.agent_version_id, s.agent_name, s.channel, s.type, "
        "s.status, s.close_reason, "
        "COALESCE(s.started_at, s.created_at) AS started_at, s.ended_at, s.userdata, "
        "s.duration_s, s.from_e164, s.to_e164, s.conversation_id, s.conversation_ref_id, "
        "s.trigger_id, s.integration_id, s.phone_number_id, s.telephony_account_id, "
        "s.stream_connection_id, s.idempotency_key, "
        "sc.name AS stream_connection_name, sc.dialect AS stream_dialect, "
        "s.livekit_room, s.error, "
        "s.provider_cost, s.platform_fee, s.total_charge, "
        "s.pricing_snapshot, s.billing_status, s.billed_at, s.tenant_id, "
        "s.recording_status, s.recording_started_at, s.recording_duration_s, "
        "s.recording_bytes, s.recording_object_key, s.content_deleted_at, "
        "s.screenshare_recording_status, s.screenshare_recording_started_at, "
        "s.screenshare_recording_duration_s, s.screenshare_recording_bytes, "
        "s.screenshare_recording_object_key, "
        # When the organization's retention policy will delete this call's
        # content. Read from the job that will do it rather than recomputed from
        # today's policy: changing `retention_days` moves nothing already
        # scheduled, so arithmetic here would state a date that will not happen.
        # MIN because deleting the call schedules a second, sooner purge beside
        # the retention one rather than cancelling it.
        "(SELECT min(j.scheduled_at) FROM scheduled_jobs j "
        "  WHERE j.tenant_id = s.tenant_id AND j.subject_id = s.id "
        "    AND j.kind = 'session.purge' AND j.status = 'pending') AS content_expires_at, "
        "s.agent_plan, "
        "s.vars AS session_vars, "
        "s.analysis_status, s.analysis_skip_reason, s.summary, s.outcome, "
        "s.outcome_rationale, s.analysis_fields, s.transfer, "
        "ref.conversation_key AS contact_key, ref.userdata AS contact_userdata, "
        "av.version AS agent_version, av.config AS version_config "
        "FROM sessions s "
        "LEFT JOIN agent_versions av "
        "  ON av.id = s.agent_version_id AND av.tenant_id = s.tenant_id "
        # LEFT because a connection can be deleted while its calls are kept —
        # the FK is SET NULL, so the call outlives the integration that carried
        # it and still reports its dialect from the trace below.
        "LEFT JOIN stream_connections sc "
        "  ON sc.id = s.stream_connection_id AND sc.tenant_id = s.tenant_id "
        # The person on the call, reached the same way `session.completed` reaches
        # it (webhooks/payloads.py): through `conversations.conversation_ref_id`,
        # the invariant edge, rather than the nullable convenience column on the
        # session. LEFT because a refused or off-thread call has no identity.
        "LEFT JOIN conversations c "
        "  ON c.id = s.conversation_id AND c.tenant_id = s.tenant_id "
        "LEFT JOIN conversation_refs ref "
        "  ON ref.id = c.conversation_ref_id AND ref.tenant_id = c.tenant_id "
        "WHERE s.id = $1 AND s.tenant_id = $2 AND s.channel IN ('voice', 'video')",
        session_id,
        ctx.tenant.id,
    )
    if not session:
        raise HTTPException(status_code=404, detail="call not found")
    reads = await load_session_reads(pool, ctx.tenant.id, session_id)
    events = reads.events

    # A version is null for a draft and for an inline agent alike; the plan is
    # what tells them apart, and a draft is reported as the "draft" it was asked
    # for.
    drafts = draft_agent_ids(session["agent_plan"])

    def version_ran(agent_id: object, version: int | None) -> int | Literal["draft"] | None:
        return "draft" if version is None and agent_id and str(agent_id) in drafts else version

    # What this call actually ran. A plan is re-resolved rather than re-merged by
    # every client: the base is immutable and so is the override, so this can
    # only ever produce what the worker produced.
    agent_plan = session["agent_plan"]
    team: list[CallTeamMemberResponse] = []
    resolved_config: AgentConfig | None = None
    if agent_plan:
        try:
            roster = await load_plan_roster(pool, ctx.tenant.id, agent_plan)
        except (ValueError, ValidationError):
            # The base version was deleted after the call. The call still
            # happened and everything else about it is still worth reading.
            logger.exception("call %s: agent plan can no longer be resolved", session_id)
            roster = []
        if roster:
            resolved_config = roster[0].config
            team = [
                CallTeamMemberResponse(
                    name=m.name,
                    agent_id=UUID(m.agent_id) if m.agent_id else None,
                    version=version_ran(m.agent_id, m.version),
                )
                for m in roster
            ]
    elif isinstance(session["version_config"], dict):
        resolved_config = AgentConfig.model_validate(session["version_config"])
        team = [
            CallTeamMemberResponse(
                name=resolved_config.name,
                agent_id=session["agent_id"],
                version=session["agent_version"],
            )
        ]
    recording = _recording_from_row(session)
    recording.expires_at = session["content_expires_at"]
    screen_recording = _screenshare_from_row(session)
    screen_recording.expires_at = session["content_expires_at"]
    # Detail only: the player needs a URL it can hand straight to an <audio> or
    # <video> element and to a Download control, and both die within the hour.
    await attach_recording_links(recording, session, session_id, ctx)
    await attach_screen_recording_links(screen_recording, session, session_id, ctx)
    return CallDetailResponse(
        session=CallSessionResponse(
            id=session["id"],
            agent=AgentMetadata(
                agent_id=session["agent_id"],
                name=session["agent_name"],
                channel=session["channel"],
            ),
            type=session["type"],
            status=_api_status(session["status"]),
            close_reason=session["close_reason"],
            close_reason_label=close_reasons.describe(session["close_reason"])
            if session["close_reason"]
            else None,
            started_at=session["started_at"],
            ended_at=session["ended_at"],
            from_e164=session["from_e164"],
            to_e164=session["to_e164"],
            conversation_id=session["conversation_id"],
            userdata=session["userdata"],
            duration_s=session["duration_s"],
            recording=recording,
            screen_recording=screen_recording,
            transfer=(
                TransferResponse.model_validate(session["transfer"])
                if isinstance(session["transfer"], dict)
                else None
            ),
            stream=_stream_from(session, events),
            contact_key=session["contact_key"],
            contact_userdata=session["contact_userdata"],
            livekit_room=session["livekit_room"],
            conversation_ref_id=session["conversation_ref_id"],
            trigger_id=session["trigger_id"],
            integration_id=session["integration_id"],
            phone_number_id=session["phone_number_id"],
            telephony_account_id=session["telephony_account_id"],
            agent_version=version_ran(session["agent_id"], session["agent_version"]),
            billing_status=session["billing_status"],
            billed_at=session["billed_at"],
            analysis=analysis_from_row(session),
            error=session["error"],
            content_deleted_at=session["content_deleted_at"],
        ),
        snapshot=build_health_snapshot(
            RunFacts.from_session(session), reads.transcript, events, reads.usage
        ),
        transcript=await items_out(pool, ctx.tenant.id, reads.transcript, events=events),
        usage=UsageResponse(
            llm=[LLMUsageResponse.model_validate(dict(r)) for r in reads.usage["llm"]],
            tts=[TTSUsageResponse.model_validate(dict(r)) for r in reads.usage["tts"]],
            stt=[STTUsageResponse.model_validate(dict(r)) for r in reads.usage["stt"]],
            realtime=[
                RealtimeUsageResponse.model_validate(dict(r)) for r in reads.usage["realtime"]
            ],
            avatar=[AvatarUsageResponse.model_validate(dict(r)) for r in reads.usage["avatar"]],
        ),
        cost=detail_cost(session),
        events=[SessionEventResponse.model_validate(dict(e)) for e in events],
        tools=await _config_tools(pool, ctx.tenant.id, resolved_config),
        starting_prompt=resolved_config.prompt if resolved_config else "",
        greeting=resolved_config.greeting if resolved_config else None,
        agent_config=resolved_config,
        agent_plan=agent_plan,
        vars=session["session_vars"] or {},
        team=team,
    )


# ── recording ───────────────────────────────────────────────────────────────
#
# The audio is served *by the bucket*, through a presigned URL; the API only
# decides whether to mint one. Every operation below starts by resolving the
# recording's state, so "the list said it was there" and "the player 403s"
# cannot disagree — there is one rule and it lives in services.recordings.


async def _load_recording_row(session_id: UUID, ctx: Context) -> asyncpg.Record:
    """Load one call's recording columns, scoped to the caller's tenant.

    `tenant_id` in the WHERE is the isolation: a session id belonging to another
    tenant simply does not exist here, and answers 404 like any unknown id rather
    than confirming that someone else's call is out there.
    """
    pool = await ctx.tenant_pool()
    row = await pool.fetchrow(
        "SELECT id, recording_status, recording_object_key, recording_started_at, "
        "recording_duration_s, recording_bytes, "
        "screenshare_recording_status, screenshare_recording_object_key, "
        "screenshare_recording_started_at, ended_at "
        "FROM sessions WHERE id = $1 AND tenant_id = $2 AND channel IN ('voice', 'video')",
        session_id,
        ctx.tenant.id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="call not found")
    return row


def _tenant_object_key(row: asyncpg.Record, column: str, ctx: Context) -> str | None:
    """The row's object key, refusing to return one outside the caller's prefix.

    The row was already fetched with `tenant_id` in the WHERE, so a key from
    another tenant should be unreachable. This checks anyway, because the thing
    on the other end is a recording of someone's customer talking — or of their
    screen: if a bad key ever gets written, the failure must be a loud 500
    rather than one tenant quietly streaming another's call.
    """
    key = row[column]
    if not key:
        return None
    expected_prefix = f"{ctx.tenant.id}/"
    if not key.startswith(expected_prefix):
        raise HTTPException(status_code=500, detail="recording is stored under an unexpected key")
    return str(key)


# Why each non-playable state has its own sentence: "no recording" for five
# different reasons is the difference between a user understanding their own
# settings and filing a support ticket.
_UNAVAILABLE_REASON: dict[RecordingState, str] = {
    RecordingState.NONE: "this call was not recorded — recording is off for this agent",
    RecordingState.NOT_SHARED: "nobody shared their screen on this call",
    RecordingState.PENDING: "the call is still in progress; its recording is not ready yet",
    RecordingState.EXPIRED: (
        "this recording was deleted under your organization's data retention policy"
    ),
    RecordingState.CONSENT_WITHDRAWN: "the caller asked not to be recorded",
    RecordingState.DELETED: "this recording was deleted",
    RecordingState.FAILED: "this call's recording could not be saved",
}


# The two artifacts a call can produce, so everything below signs, deletes and
# reports them through one path. Each names its own columns and its own download
# filename; nothing else about them differs.
@dataclass(frozen=True)
class _Artifact:
    status_column: str
    started_at_column: str
    key_column: str
    download_filename: Callable[[UUID], str]


_AUDIO = _Artifact(
    status_column="recording_status",
    started_at_column="recording_started_at",
    key_column="recording_object_key",
    download_filename=recordings.download_filename,
)
_SCREEN = _Artifact(
    status_column="screenshare_recording_status",
    started_at_column="screenshare_recording_started_at",
    key_column="screenshare_recording_object_key",
    download_filename=recordings.screenshare_download_filename,
)


async def _presigned_artifact_url(
    row: asyncpg.Record, artifact: _Artifact, session_id: UUID, ctx: Context, *, download: bool
) -> str:
    """Sign a link to one of a call's recordings, refusing every non-playable state.

    `resolve_state` gates the minting, so a signed URL is never handed out for a
    recording the rest of the API describes as gone, pending or never made.
    """
    state = recordings.resolve_state(
        row[artifact.status_column],
        row[artifact.started_at_column],
        session_ended_at=row["ended_at"],
    )
    if state is not RecordingState.AVAILABLE:
        raise HTTPException(status_code=404, detail=_UNAVAILABLE_REASON[state])

    key = _tenant_object_key(row, artifact.key_column, ctx)
    if not key:
        # AVAILABLE without a key means the worker wrote 'stored' and lost the
        # key — a bug, not an empty state, so it must not read as "no recording".
        raise HTTPException(status_code=500, detail="recording is stored but has no object key")

    return await recordings.presigned_url(
        key=key,
        expires_in=int(recordings.PRESIGNED_TTL.total_seconds()),
        attachment_filename=artifact.download_filename(session_id) if download else None,
    )


async def call_recording_url(session_id: UUID, ctx: Context, *, download: bool) -> str:
    """Where one call's audio actually lives, signed for a short window."""
    row = await _load_recording_row(session_id, ctx)
    return await _presigned_artifact_url(row, _AUDIO, session_id, ctx, download=download)


async def call_screen_recording_url(session_id: UUID, ctx: Context, *, download: bool) -> str:
    """Where one call's screen video actually lives, signed for a short window."""
    row = await _load_recording_row(session_id, ctx)
    return await _presigned_artifact_url(row, _SCREEN, session_id, ctx, download=download)


async def _attach_links(
    recording: RecordingResponse,
    artifact: _Artifact,
    row: asyncpg.Record,
    session_id: UUID,
    ctx: Context,
) -> None:
    """Fill in the detail view's play and download links, in place.

    Silent when the recording is not playable: the caller is rendering a whole
    call, and `state` has already said why there is nothing to play. A bucket
    that cannot be signed against is different — that is a configuration fault,
    and it fails the request rather than serving a call whose player is
    inexplicably missing.
    """
    if recording.state is not RecordingState.AVAILABLE:
        return
    recording.url = await _presigned_artifact_url(row, artifact, session_id, ctx, download=False)
    recording.download_url = await _presigned_artifact_url(
        row, artifact, session_id, ctx, download=True
    )
    recording.url_expires_at = datetime.now(UTC) + recordings.PRESIGNED_TTL


async def attach_recording_links(
    recording: RecordingResponse, row: asyncpg.Record, session_id: UUID, ctx: Context
) -> None:
    await _attach_links(recording, _AUDIO, row, session_id, ctx)


async def attach_screen_recording_links(
    recording: RecordingResponse, row: asyncpg.Record, session_id: UUID, ctx: Context
) -> None:
    await _attach_links(recording, _SCREEN, row, session_id, ctx)


async def delete_call(
    session_id: UUID,
    ctx: Context,
    *,
    channels: Sequence[str] = ("voice", "video"),
    noun: str = "call",
) -> None:
    """Erase one call's content. The call itself stays, with its cost.

    ``channels`` and ``noun`` are what make this the erase behind
    ``DELETE /v1/chats/{id}`` too: a chat is a text session, erased the same way.

    Schedules the same `session.purge` job retention uses, for now rather than
    for a deadline — one code path, and the reason a request to delete 200 000
    calls does not arrive as 200 000 synchronous multi-table deletes.

    A recording already deleted through `DELETE /v1/calls/{id}/recording` is not
    a special case: the job treats an absent object as a no-op, which is cheaper
    and safer than making two endpoints know about each other.

    **A live call is refused.** The purge runs within a scheduler tick, and a
    call still in progress goes on writing: finalize would put the userdata, the
    summary and the analysis back on a row already stamped `content_deleted_at`,
    so the receipt would be a lie. Worse, the cascade would read the live
    session as purged, take its conversation, and leave the rest of the call
    writing turns against a conversation that no longer exists. Retention never
    hits this — it schedules from `ended_at` — so the guard belongs here, on the
    one path that can ask for a purge *now*.
    """
    pool = await ctx.tenant_pool()
    row = await pool.fetchrow(
        "SELECT status FROM sessions "
        "WHERE id = $1 AND tenant_id = $2 AND channel = ANY($3::text[])",
        session_id,
        ctx.tenant.id,
        list(channels),
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"{noun} not found")
    if row["status"] in ("queued", "running"):
        raise HTTPException(
            status_code=409,
            detail=f"this {noun} is still in progress; erase it once it has ended",
        )
    await jobs.enqueue(
        pool,
        tenant_id=ctx.tenant.id,
        kind=jobs.JobKind.SESSION_PURGE,
        subject_id=session_id,
        scheduled_at=datetime.now(UTC),
        args={"session_id": str(session_id)},
    )


# Which column each artifact's delete writes. Kept beside the statement it
# parameterizes rather than on `_Artifact`, because it is the one thing here
# that is a write rather than a description of where the bytes are.
_DELETE_STATEMENT: dict[str, str] = {
    _AUDIO.key_column: """
        UPDATE sessions
        SET recording_status = $3,
            recording_object_key = NULL,
            recording_bytes = NULL,
            updated_at = now()
        WHERE id = $1 AND tenant_id = $2
    """,
    _SCREEN.key_column: """
        UPDATE sessions
        SET screenshare_recording_status = $3,
            screenshare_recording_object_key = NULL,
            screenshare_recording_bytes = NULL,
            updated_at = now()
        WHERE id = $1 AND tenant_id = $2
    """,
}


async def delete_call_recording(session_id: UUID, ctx: Context) -> None:
    """Delete one call's media. The transcript and everything else stay.

    Both artifacts go together — the audio and, where the agent watched one, the
    screen video. They are one thing to the person clicking Delete, and leaving
    the screen behind because the control was named after the audio is the kind
    of gap nobody discovers until it matters.

    Quiet on anything that is not playable, which is more than "already
    deleted": there is no object behind `none`, `not_shared`, `failed` or
    `consent_withdrawn`, and each of those already says something truer than
    `deleted` would. Overwriting `consent_withdrawn` in particular would destroy
    a compliance record — that the caller asked — and replace it with somebody's
    click. `expired` is the same case for the tenant's own retention policy.

    A recording still uploading (`pending`) is the one gap: nothing is stored to
    delete yet, and finalize will write it a moment later. Ask again once the
    call has settled.
    """
    row = await _load_recording_row(session_id, ctx)
    pool = await ctx.tenant_pool()
    for artifact in (_AUDIO, _SCREEN):
        state = recordings.resolve_state(
            row[artifact.status_column],
            row[artifact.started_at_column],
            session_ended_at=row["ended_at"],
        )
        if state is not RecordingState.AVAILABLE:
            continue

        key = _tenant_object_key(row, artifact.key_column, ctx)
        if key:
            # S3 DELETE is idempotent, so an object something else already
            # removed costs one no-op call and the row still transitions rather
            # than being left claiming something is out there.
            await recordings.delete_object(key=key)

        await pool.execute(
            _DELETE_STATEMENT[artifact.key_column],
            session_id,
            ctx.tenant.id,
            RecordingStatus.DELETED.value,
        )
