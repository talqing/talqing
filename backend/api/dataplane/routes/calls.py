"""Calls HTTP adapters: mint dispatch token + list/inspect voice/video sessions."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Body, Query
from fastapi.responses import RedirectResponse, Response

from api.core.schemas import Page
from api.dataplane.deps import Context, CtxDep, WriteCtxDep
from services import calls as svc
from services.calls.analysis_ops import (
    BackfillAnalysisRequest,
    BackfillAnalysisResponse,
    PreviewAnalysisRequest,
    PreviewAnalysisResponse,
)
from services.calls.service import (
    CallDetailResponse,
    CallStatsResponse,
    CallSummaryResponse,
    CallTokenResponse,
    CallType,
    TokenRequest,
)

router = APIRouter(prefix="/calls", tags=["calls"])


@router.post("/token", response_model=CallTokenResponse)
async def calls_token(
    body: TokenRequest = Body(...),
    ctx: Context = WriteCtxDep,
) -> CallTokenResponse:
    """Mint a LiveKit token for a browser to open a web call with an agent.

    `userdata` seeds the session state read as `{{userdata.field}}`; keys
    beginning with `_talqing` are reserved. Prefer `agent_id`; reach for
    `agent_override`, `agent` or `agent_team` only when the definition is
    generated per request and would never be reused.

    Needs the editor role — these fields run an arbitrary prompt on an arbitrary
    model, paid for with the workspace's own provider keys.

    The token only lets a browser connect. To dial a phone, use
    `create_outbound_call`.
    """
    return await svc.calls_token(body, ctx)


@router.get("", response_model=Page[CallSummaryResponse])
async def list_calls(
    ctx: Context = CtxDep,
    limit: int = Query(200, ge=1, le=200),
    offset: int = Query(0, ge=0),
    start: datetime | None = Query(default=None, description="window start (default: 30d ago)"),
    end: datetime | None = Query(default=None, description="window end (default: now)"),
    agent_id: UUID | None = Query(default=None),
    type: CallType | None = Query(default=None, description="how the call reached the agent"),
    status: str | None = Query(
        default=None, description="queued, in_progress, completed, failed or canceled"
    ),
    outcome: str | None = Query(
        default=None,
        description="success, failure or unknown — whether the call achieved its purpose, "
        "as judged by post-call analysis",
    ),
    contact_key: str | None = Query(
        default=None,
        description="your stable id for the person on the call — every call with that "
        "one customer, however many numbers they rang from",
    ),
    close_reason: str | None = Query(
        default=None,
        description="exactly how the call ended, e.g. participant_disconnected, "
        "transferred, sip_user_unavailable_failed — the same strings the "
        "observability endings breakdown ranks",
    ),
    batch_id: UUID | None = Query(
        default=None,
        description="only calls placed by this outbound batch, retries included",
    ),
) -> Page[CallSummaryResponse]:
    """List voice and video calls, newest first, with status, duration and cost.

    The window defaults to the last 30 days. Filter by `agent_id`, by `type` to
    separate web calls from phone calls, by `contact_key` for one customer's
    whole history, or by `batch_id` for every call one outbound batch placed.
    Text conversations are not calls — those live under `list_conversations`.
    """
    return await svc.list_calls(
        ctx,
        limit=limit,
        offset=offset,
        start=start,
        end=end,
        agent_id=agent_id,
        type_=type,
        outcome=outcome,
        status=status,
        contact_key=contact_key,
        close_reason=close_reason,
        batch_id=batch_id,
    )


@router.get("/stats", response_model=CallStatsResponse)
async def call_stats(
    ctx: Context = CtxDep,
    start: datetime | None = Query(default=None, description="window start (default: 30d ago)"),
    end: datetime | None = Query(default=None, description="window end (default: now)"),
    agent_id: UUID | None = Query(default=None),
) -> CallStatsResponse:
    """Totals for a window: call count, minutes, spend and average cost.

    Spend and `avg_cost_per_call` count only priced calls (`priced_calls`), so
    a call still being priced does not drag the average down. The window
    defaults to the last 30 days.
    """
    return await svc.call_stats(ctx, start=start, end=end, agent_id=agent_id)


# Declared BEFORE /{session_id} so the literal path isn't captured as a UUID.
@router.post("/analysis/backfill", response_model=BackfillAnalysisResponse)
async def backfill_call_analysis(
    body: BackfillAnalysisRequest = Body(...),
    ctx: Context = WriteCtxDep,
) -> BackfillAnalysisResponse:
    """Re-analyse past calls with each one's current agent definition.

    Use this after changing what an agent extracts, to fill in calls that ran
    under the old definition — and to retry calls whose analysis failed.

    Each call is analysed by a real model call and re-priced, so the tenant is
    charged again for every call in the list. Try the definition on one call
    with `preview_call_analysis` first.
    """
    return await svc.backfill_call_analysis(body, ctx)


@router.post("/{session_id}/analysis/preview", response_model=PreviewAnalysisResponse)
async def preview_call_analysis(
    session_id: UUID,
    body: PreviewAnalysisRequest = Body(...),
    ctx: Context = WriteCtxDep,
) -> PreviewAnalysisResponse:
    """Try an analysis definition against one past call and see what it finds.

    Nothing is saved, which makes this the way to tune a summary prompt, a
    success definition or an extraction field before putting it on the agent.
    The call is analysed against the agent definition it actually ran. Costs one
    model call.
    """
    return await svc.preview_call_analysis(session_id, body, ctx)


@router.get("/{session_id}", response_model=CallDetailResponse)
async def get_call(session_id: UUID, ctx: Context = CtxDep) -> CallDetailResponse:
    """Everything about one call: health, transcript, tool calls, usage, cost.

    Start with `snapshot`: it answers "did this call go well?" on its own —
    `snapshot.issues` names every problem found and `snapshot.ok` is true when
    there were none.

    `transcript` carries what was said with every function call and its output,
    and `session.userdata` is the state the call ended with. `tools`,
    `starting_prompt` and `greeting` are the frozen version this call actually
    ran, not the agent's current draft.
    """
    return await svc.get_call(session_id, ctx)


@router.delete(
    "/{session_id}",
    status_code=202,
    # Accepted, with nothing to say back: the erase is scheduled, and there is
    # no job to hand the caller. Without this FastAPI serializes the `None`
    # below and the body is a literal `null` under an untyped schema.
    response_class=Response,
    responses={202: {"description": "Erase scheduled. No body."}},
)
async def delete_call(session_id: UUID, ctx: Context = WriteCtxDep) -> Response:
    """Erase this call's content. The call itself stays, with its cost.

    What goes: the recording, the transcript, every tool call and its output,
    the session's userdata, the summary, the extracted fields, and the phone
    numbers on the call. What stays: the call, its duration, status, outcome,
    per-turn timings and everything it was billed — so past invoices still
    resolve.

    Erasing a person's last remaining call also erases what your agents
    remember about them, so the next call from that number meets an agent that
    has never heard of them.

    Returns `202`: the work is scheduled rather than done, which is what stops a
    request to erase two hundred thousand calls arriving as two hundred thousand
    synchronous deletes. It normally completes within seconds. This cannot be
    undone.

    A call that is still in progress is refused with a `409` — it is still
    writing, and finalize would put the content back on a row already marked
    erased. Ask again once it has ended.

    To delete only the audio and keep the transcript, use
    `delete_call_recording` instead — that one is immediate.
    """
    await svc.delete_call(session_id, ctx)
    return Response(status_code=202)


@router.get(
    "/{session_id}/recording",
    status_code=302,
    response_class=RedirectResponse,
    responses={
        302: {"description": "Redirect to a short-lived link to the audio in object storage"},
        # What a client actually ends up holding. Every HTTP client follows a
        # redirect by default, so the observable outcome of calling this is the
        # audio itself, and a generated SDK types its return value off this
        # entry. Documenting only the 302 leaves that return value `unknown`.
        200: {
            "description": "The audio, once the redirect above has been followed",
            "content": {"audio/ogg": {"schema": {"type": "string", "format": "binary"}}},
        },
    },
)
async def get_call_recording(
    session_id: UUID,
    ctx: Context = CtxDep,
    download: bool = Query(
        False,
        description="serve as a file named call-{id}.ogg rather than inline",
    ),
) -> RedirectResponse:
    """Redirect to the call's audio: one stereo Ogg/Opus file.

    The redirect goes to object storage and expires within the hour, so follow
    it straight away rather than storing it. It supports `Range`, which is what
    lets a player seek without downloading the whole call first. Pass
    `download=true` for a link that saves as `call-{id}.ogg`.

    The caller is the left channel and the agent is the right, so you can listen
    to either side alone by splitting the channels — one `ffmpeg -map_channel`
    away, and what makes the file useful both for hearing the call and for
    building training data. It is what the agent heard after noise cancellation,
    not a capture of the phone line, and it does not include background audio,
    which the agent publishes as its own track.

    A `404` here means there is nothing to play, and its message says why — the
    agent had recording off, the call is still running, the recording was
    deleted by a person or by your retention policy, the caller asked not to be
    recorded, or it failed to save. Check `recording.state` on the call first to
    know which without a round trip; `get_call` also returns ready-made
    `recording.url` and `recording.download_url` links.
    """
    return RedirectResponse(
        await svc.call_recording_url(session_id, ctx, download=download), status_code=302
    )


@router.get(
    "/{session_id}/screen-recording",
    status_code=302,
    response_class=RedirectResponse,
    responses={
        302: {
            "description": "Redirect to a short-lived link to the screen video in object storage"
        },
        200: {
            "description": "The video, once the redirect above has been followed",
            "content": {"video/mp4": {"schema": {"type": "string", "format": "binary"}}},
        },
    },
)
async def get_call_screen_recording(
    session_id: UUID,
    ctx: Context = CtxDep,
    download: bool = Query(
        False,
        description="serve as a file named call-{id}-screen.mp4 rather than inline",
    ),
) -> RedirectResponse:
    """Redirect to the screen the person shared: one 1 fps H.264 file.

    Only agents with `vision_input.screenshare.record` produce one. It covers the
    whole call rather than only the shared stretches — the parts where nobody was
    sharing are black — so its timeline lines up with the audio and the
    transcript second for second, and a turn's offset into it is the same
    arithmetic as into the recording.

    Like the audio, the redirect goes to object storage, expires within the hour
    and supports `Range`. A `404` says why there is nothing to play, and
    `screen_recording.state` on the call answers the same question without a
    round trip — including `not_shared`, which means screen recording was on and
    nobody ever shared, as opposed to `none`, which means it was off.
    """
    return RedirectResponse(
        await svc.call_screen_recording_url(session_id, ctx, download=download), status_code=302
    )


@router.delete("/{session_id}/recording", status_code=204)
async def delete_call_recording(session_id: UUID, ctx: Context = WriteCtxDep) -> None:
    """Delete the call's recorded media, keeping the call itself.

    Both the audio and, where the agent watched one, the screen video. The
    transcript, cost and everything else about the call stay exactly as they are.
    This is immediate and cannot be undone. Deleting a recording that is already
    gone succeeds quietly.

    Deliberately synchronous, unlike `delete_call`: this destroys one or two
    objects and returns, where deleting a call schedules a redaction across
    several tables.
    """
    await svc.delete_call_recording(session_id, ctx)
