"""A health verdict: cost, latency distribution, tooling, connection.

Computed over one call (a single `sessions` row) or a whole conversation (every
session in the thread) — `RunFacts` is what absorbs the difference, so both
surfaces read the same numbers by the same rules.

Everything is derived from rows the caller has already loaded: the transcript
(`conversation_items`, which carries LiveKit's per-message `MetricsReport`), the
usage rollups, the session row(s), and the `session_events` trace. The only
query it costs is the caller's own.

The point of the verdict is that someone opening a call should not have to read
the transcript to find out whether it went badly. `issues` is the list; `ok` is
just "the list is empty".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import asyncpg
from pydantic import BaseModel, ConfigDict

from services.session_events import (
    AGENT_FALSE_INTERRUPTION,
    AGENT_READY,
    CONNECTION_QUALITY_RANK,
    HOLD_ENDED,
    NOISE_CANCELLATION_FAILED,
    PROVIDER_FAILED,
    PROVIDER_RECOVERED,
    RTC_QUALITY,
    SESSION_ERROR,
    TOOL_ENDED,
    TOOL_STARTED,
)
from utils.latency import (
    STAGE_ORDER,
    LatencyStage,
    ReplyLatency,
    item_data,
    latency_stats,
    reply_breakdowns,
)

# A response that takes longer than this to start is the thing users notice
# first. Shipped to the client in the snapshot so the chart's threshold rule and
# the issue text below can never disagree about where the line is.
SLOW_RESPONSE_MS = 3500.0

# A tool the caller waited on for longer than this. Same class of defect as a
# slow response and the most common reason somebody hangs up mid-call, so it
# gets the same treatment: a threshold here, shipped to the client, and one
# issue line. Higher than SLOW_RESPONSE_MS because a tool is *expected* to take
# a moment — this is the point at which the silence stops being explainable.
SLOW_TOOL_MS = 8000.0


IssueKind = Literal[
    "failed",
    "never_ready",
    "provider_errors",
    "provider_failover",
    "tool_failures",
    "tools_unfinished",
    "slow_tools",
    "slow_responses",
    "slow_after_filler",
    "connection",
    "noise_cancellation",
    "unpriceable",
]


class HealthIssue(BaseModel):
    """One problem found, and what kind it is."""

    kind: IssueKind
    message: str


class LatencyStats(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    avg_ms: float | None = None
    p95_ms: float | None = None
    max_ms: float | None = None
    samples: int = 0


class PaceStats(BaseModel):
    """How the call *sounded*, as opposed to how fast the stack was.

    Derived from `started_speaking_at` / `stopped_speaking_at`, which LiveKit
    puts on every message and nothing has ever read. None of it is a platform
    defect — a long reply is the prompt's doing and a long silence is often the
    caller thinking — which is why it lands here as numbers rather than in
    `issues`.
    """

    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    # Longest single uninterrupted agent reply. The number that says "your agent
    # monologued", which no latency metric can show.
    longest_agent_turn_ms: float | None = None
    # No `longest_silence_ms`. It computed
    # `agent.started_speaking_at − user.stopped_speaking_at` and claimed to be
    # something `e2e_latency` is not — but `agent_activity.py` defines
    # `e2e_latency` as that exact subtraction, so it was a second, strictly
    # narrower copy of `response_latency.max_ms` (narrower because it only
    # compares adjacent turns, and so misses the long waits that follow a tool
    # call — the very ones worth seeing). Nothing ever read it.
    # Turns where both were speaking at once. Distinct from `interruptions`,
    # which only counts the times the agent yielded.
    talk_over_turns: int = 0
    # Total time parked on hold music during a bridged transfer. Inside
    # `duration_seconds`, inside the recording, inside the bill — and silent.
    hold_ms: float | None = None


class ConnectionQuality(BaseModel):
    """The worst reading on each side of the call, kept apart on purpose.

    A collapsed "the connection was poor" reads as a platform fault even when
    the poor leg was the caller's own network, which is the difference between
    a support ticket and a shrug. `overall` stays for readers that only want one
    word; `agent` and `caller` are `unknown` on calls whose trace predates the
    `side` field.
    """

    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    overall: str = "unknown"
    agent: str = "unknown"
    caller: str = "unknown"


class HealthSnapshotResponse(BaseModel):
    """The band at the top of the call and conversation views."""

    # Response-only, and every field below is always sent. Without this, a
    # field with a default is absent from `required` in the OpenAPI document,
    # so every generated client types it optional and every caller narrows a
    # value that is never missing. Safe here precisely because nothing parses
    # this model as a request.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    duration_seconds: int | None = None
    slow_response_threshold_ms: float = SLOW_RESPONSE_MS
    slow_tool_threshold_ms: float = SLOW_TOOL_MS

    # conversation shape. `turns` counts the caller's; what the agent said
    # unprompted (a greeting, a check-in) is in `agent_messages`.
    turns: int = 0
    user_messages: int = 0
    agent_messages: int = 0
    interruptions: int = 0
    uninterrupted_responses: int = 0
    pace: PaceStats = PaceStats()

    # response time: the end of caller speech to the agent's first audio, or a
    # typed message arriving to the first token back
    response_latency: LatencyStats = LatencyStats()
    # Where the average response went: each stage's share of `avg_ms`, tools and
    # the model call that chose them included, so the stages add up to it.
    response_stages: list[LatencyStage] = []

    # money and metering
    total_cost_usd: float | None = None
    billing_status: str = "pending"
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    stt_audio_ms: int = 0
    tts_audio_ms: int = 0
    total_audio_ms: int = 0
    tts_characters: int = 0
    providers: list[str] = []

    # tooling
    tool_calls: int = 0
    tool_call_failures: int = 0
    # Started and never reported an ending, because the run died while the tool
    # was still going. Distinct from a failure: nobody said it went wrong.
    unfinished_tool_calls: int = 0
    # Tools the caller actually waited longer than `slow_tool_threshold_ms` on.
    # A background tool is never one of them, however long its work ran: it
    # hands the turn back at dispatch.
    slow_tool_calls: int = 0
    # The subset of `slow_tool_calls` the caller actually sat through in
    # silence. A tool tree can contain `say` / `generate_reply` operations, so a
    # slow tool is only a defect when nothing was being spoken over it.
    slow_tool_calls_in_silence: int = 0
    # Replies that came more than `slow_response_threshold_ms` after the agent's
    # own filler line ("let me check"), with a tool running in between.
    slow_replies_after_filler: int = 0

    # platform health, from the session_events trace
    errors: int = 0
    provider_failures: int = 0
    provider_recoveries: int = 0
    agent_became_ready: bool = False
    # The worst reading across every participant. Kept as a bare string because
    # every existing reader is written against it; `connection` splits it by side.
    connection_quality: str = "unknown"
    connection: ConnectionQuality = ConnectionQuality()
    # A speech-pipeline misfire: the agent stopped for something that was not an
    # utterance. `resumed` counts the ones that self-corrected, which is why this
    # is a tile and not an issue.
    false_interruptions: int = 0
    false_interruptions_resumed: int = 0
    noise_cancellation_failed: bool = False

    ok: bool = True
    issues: list[HealthIssue] = []


@dataclass(frozen=True)
class RunFacts:
    """The session-level facts a snapshot needs, however many runs produced them.

    One call is one `sessions` row; one conversation is every run in the thread.
    Folding the difference into this small struct is what lets both surfaces
    share the verdict logic instead of growing two copies that drift.
    """

    duration_seconds: int | None
    billing_status: str
    total_cost_usd: float | None
    # True when the run (or any run in the thread) ended in `failed`.
    failed: bool
    # True once nothing more will be added — a running call is not yet judged
    # on "the agent never became ready".
    terminal: bool
    close_reason: str | None
    # What the issue text calls this thing.
    noun: str = "call"

    @classmethod
    def from_session(cls, session: asyncpg.Record) -> RunFacts:
        duration = session["duration_s"]
        if duration is None and session["started_at"] is not None:
            end = session["ended_at"] or datetime.now(UTC)
            duration = max(round((end - session["started_at"]).total_seconds()), 0)
        billing_status = session["billing_status"]
        return cls(
            duration_seconds=duration,
            billing_status=billing_status,
            total_cost_usd=(
                float(session["total_charge"])
                if billing_status == "computed" and session["total_charge"] is not None
                else None
            ),
            failed=session["status"] == "failed",
            terminal=session["status"] in ("completed", "failed", "canceled"),
            close_reason=session["close_reason"],
        )

    @classmethod
    def from_runs(cls, runs: Sequence[asyncpg.Record]) -> RunFacts:
        """Fold every run in a conversation into one set of facts.

        Cost sums only the priced runs, and stays None when none are priced —
        a partly-priced thread would otherwise report a total that silently
        omits the rest.
        """
        priced = [r for r in runs if r["billing_status"] == "computed" and r["total_charge"]]
        failed = next((r for r in runs if r["status"] == "failed"), None)
        billing_status = (
            "unpriceable"
            if any(r["billing_status"] == "unpriceable" for r in runs)
            else "computed"
            if priced and len(priced) == len(runs)
            else "pending"
        )
        return cls(
            duration_seconds=sum(int(r["duration_s"] or 0) for r in runs) or None,
            billing_status=billing_status,
            total_cost_usd=float(sum(r["total_charge"] for r in priced)) if priced else None,
            failed=failed is not None,
            terminal=all(r["status"] in ("completed", "failed", "canceled") for r in runs),
            close_reason=failed["close_reason"] if failed else None,
            noun="conversation",
        )


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _erroring_providers(error_events: Sequence[asyncpg.Record]) -> str:
    """Who raised the errors, named. Falls back when the trace did not say."""
    names = sorted(
        {
            str(source["provider"])
            for event in error_events
            if isinstance(source := event["payload"].get("source"), dict) and source.get("provider")
        }
    )
    if not names:
        return "A provider"
    return " and ".join([", ".join(names[:-1]), names[-1]] if len(names) > 1 else names)


def _is_interrupted(row: asyncpg.Record) -> bool:
    return bool(item_data(row).get("interrupted"))


def _connection_quality(events: Sequence[asyncpg.Record]) -> ConnectionQuality:
    """The worst reading overall, and the worst on each side.

    A reading with no `side` (a call whose trace predates the field) still
    counts towards `overall` — the aggregate has always been honest — but it
    cannot be attributed, so the per-side answers stay `unknown` and the issue
    text falls back to the flat sentence.
    """
    worst: dict[str, str] = {}
    for event in events:
        if event["type"] != RTC_QUALITY:
            continue
        quality = str(event["payload"].get("quality", ""))
        if quality not in CONNECTION_QUALITY_RANK:
            continue
        side = event["payload"].get("side")
        for bucket in ("overall", side if side in ("agent", "caller") else None):
            if bucket is None:
                continue
            current = worst.get(bucket)
            if not current or CONNECTION_QUALITY_RANK[quality] > CONNECTION_QUALITY_RANK[current]:
                worst[bucket] = quality
    return ConnectionQuality(
        overall=worst.get("overall", "unknown"),
        agent=worst.get("agent", "unknown"),
        caller=worst.get("caller", "unknown"),
    )


def _speech_window(row: asyncpg.Record) -> tuple[float, float] | None:
    """(start, stop) unix seconds for one message, when LiveKit reported both."""
    metrics = row["metrics"]
    if not isinstance(metrics, dict):
        return None
    start = metrics.get("started_speaking_at")
    stop = metrics.get("stopped_speaking_at")
    if not isinstance(start, int | float) or not isinstance(stop, int | float):
        return None
    return (float(start), float(stop)) if stop >= start else None


def _pace(messages: Sequence[asyncpg.Record], events: Sequence[asyncpg.Record]) -> PaceStats:
    """How the call sounded: monologues, talk-over, hold.

    Walks the messages in order and compares each one's speech window with the
    previous speaker's. Turns with no window (text, or a provider that reported
    none) simply do not contribute — an absent measurement is not a zero.
    """
    longest_turn: float | None = None
    talk_over = 0
    previous: tuple[float, float, str] | None = None

    for row in messages:
        role = row["role"]
        if role not in ("user", "assistant"):
            continue
        window = _speech_window(row)
        if window is None:
            previous = None
            continue
        start, stop = window
        if role == "assistant":
            duration_ms = (stop - start) * 1000.0
            longest_turn = max(longest_turn or 0.0, duration_ms)
        if previous is not None:
            _, prev_stop, prev_role = previous
            # Both mouths open at once. Distinct from `interrupted`, which only
            # fires when the agent actually gave way.
            if prev_role != role and start < prev_stop:
                talk_over += 1
        previous = (start, stop, role)

    hold_ms = sum(
        float(event["payload"]["duration_ms"])
        for event in events
        if event["type"] == HOLD_ENDED
        and isinstance(event["payload"].get("duration_ms"), int | float)
    )
    return PaceStats(
        longest_agent_turn_ms=round(longest_turn, 1) if longest_turn else None,
        talk_over_turns=talk_over,
        hold_ms=round(hold_ms, 1) or None,
    )


@dataclass(frozen=True)
class _ToolFacts:
    """What the call's tool calls did, reconciled across both records of them.

    Neither source is complete on its own. The transcript holds a
    `function_call` for a tool the model asked for and LiveKit refused before
    the executor ran — an unknown name, unparseable arguments — which leaves no
    trace event at all. The trace holds a `tool.started` for a call whose
    transcript rows never landed, which is what happens when a speech handle is
    torn down mid-turn. Counting from either alone under-reports, and the old
    `max(transcript_failures, trace_failures)` could report more failures than
    calls, which rendered as "-1/4 ok".
    """

    total: int = 0
    failures: int = 0
    unfinished: int = 0
    slow: int = 0
    slow_in_silence: int = 0


def _agent_speech_windows(messages: Sequence[asyncpg.Record]) -> list[tuple[float, float]]:
    """(start, stop) unix seconds for every stretch the agent was audible."""
    windows = []
    for row in messages:
        if row["role"] != "assistant":
            continue
        window = _speech_window(row)
        if window is not None:
            windows.append(window)
    return windows


def _tool_facts(
    transcript: Sequence[asyncpg.Record],
    events: Sequence[asyncpg.Record],
    messages: Sequence[asyncpg.Record],
) -> _ToolFacts:
    """Fold both records of the call's tool calls into one honest set of counts.

    Keyed by `call_id`, which both sources carry, so a tool call present in one
    and missing from the other is counted exactly once.
    """
    started: dict[str, datetime] = {}
    ended: dict[str, asyncpg.Record] = {}
    for event in events:
        call_id = event["payload"].get("call_id")
        if not isinstance(call_id, str):
            continue
        if event["type"] == TOOL_STARTED:
            started.setdefault(call_id, event["created_at"])
        elif event["type"] == TOOL_ENDED:
            ended[call_id] = event

    called: set[str] = set()
    errored: set[str] = set()
    for row in transcript:
        data = item_data(row)
        call_id = data.get("call_id")
        if not isinstance(call_id, str):
            continue
        if row["type"] == "function_call":
            called.add(call_id)
        elif row["type"] == "function_call_output" and data.get("is_error"):
            errored.add(call_id)

    every_call = called | started.keys() | ended.keys()
    failures = sum(
        1
        for call_id in every_call
        if call_id in errored
        or (call_id in ended and ended[call_id]["payload"].get("status") == "error")
    )
    # Only a tool we watched START and never saw finish. A `function_call` with
    # no trace at all was rejected before it ran and already has its error row.
    unfinished = sum(1 for call_id in started if call_id not in ended)

    # A tool tree can speak: `say` and `generate_reply` are operation kinds, and
    # with `wait_for_playback` the tool does not return until the line has
    # played, so the agent's own speech is inside the measured duration. The
    # spoken line lands in the transcript as an assistant message (LiveKit's
    # `say()` defaults to `add_to_chat_ctx=True`) carrying the window it was
    # audible for — so overlapping the two is what separates a tool that left
    # the caller in silence from one that was talking to them the whole time.
    speech = _agent_speech_windows(messages)
    slow = 0
    slow_in_silence = 0
    for call_id, event in ended.items():
        duration_ms = event["payload"].get("duration_ms")
        if not isinstance(duration_ms, int | float) or float(duration_ms) <= SLOW_TOOL_MS:
            continue
        # A background tool released the turn the moment it was dispatched, so
        # this number is how long the WORK took and not how long anyone waited —
        # the agent was free to keep talking the whole time. Counting it slow
        # would report the flag working as a defect.
        if event["payload"].get("background"):
            continue
        slow += 1
        stop = event["created_at"].timestamp()
        start = (
            started[call_id].timestamp()
            if call_id in started
            else stop - float(duration_ms) / 1000.0
        )
        if not any(spoke_from < stop and spoke_to > start for spoke_from, spoke_to in speech):
            slow_in_silence += 1

    return _ToolFacts(
        total=len(every_call),
        failures=failures,
        unfinished=unfinished,
        slow=slow,
        slow_in_silence=slow_in_silence,
    )


_AVERAGE_STAGE_LABEL = {
    "stt": "STT",
    "endpoint": "Endpoint",
    "hook": "On user turn hook",
    "llm_step": "LLM → tool call",
    "tool": "Tools",
    "llm": "LLM",
    "tts": "TTS",
    "other": "Other",
}


def _average_stages(responses: Sequence[ReplyLatency]) -> list[LatencyStage]:
    """Each stage's share of the average response, in the order they happen.

    Averaged over EVERY response, a turn without the stage counting as zero —
    which is what makes the stages add up to `response_latency.avg_ms`, and a
    tool three turns in ten waited on show as three tenths of its time.
    """
    if not responses:
        return []
    totals: dict[str, float] = {}
    for response in responses:
        for stage in response.stages:
            totals[stage.kind] = totals.get(stage.kind, 0.0) + stage.ms
    # No turn measured transcription on its own, so the leg is unsplit everywhere.
    unsplit = "stt" not in totals
    return [
        LatencyStage(
            kind=kind,
            ms=round(totals[kind] / len(responses), 1),
            label="STT + endpoint"
            if kind == "endpoint" and unsplit
            else _AVERAGE_STAGE_LABEL[kind],
        )
        for kind in STAGE_ORDER
        if kind in totals
    ]


def _slow_response_issue(responses: Sequence[ReplyLatency], p95_ms: float) -> str:
    """The slow-response line, naming tools when they are most of the wait."""
    issue = f"Slow responses: p95 time to first word was {p95_ms / 1000:.1f}s"
    slow = [r for r in responses if r.total_ms > SLOW_RESPONSE_MS]
    waited_on = [stage for r in slow for stage in r.stages if stage.kind == "tool"]
    if sum(stage.ms for stage in waited_on) * 2 <= sum(r.total_ms for r in slow):
        return issue
    names = dict.fromkeys(
        name for stage in waited_on for name in stage.label.removeprefix("Tool ").split(", ")
    )
    return f"{issue}, mostly waiting on tools ({', '.join(names)})"


def _usage_totals(usage_rows: dict[str, Sequence[asyncpg.Record]]) -> dict[str, Any]:
    """Fold the five per-(provider, model) rollups into one set of numbers.

    Realtime is not additive with the cascaded columns — a speech-to-speech
    model bills audio and text separately — so its modality splits are summed
    into the same input/output totals the UI shows, and its audio token counts
    stay separate for the callers that need to say "tokens, not duration".
    """
    llm = usage_rows["llm"]
    stt = usage_rows["stt"]
    tts = usage_rows["tts"]
    realtime = usage_rows["realtime"]

    input_tokens = sum(int(r["input_tokens"]) for r in llm)
    cached_input_tokens = sum(int(r["input_cached_tokens"]) for r in llm)
    output_tokens = sum(int(r["output_tokens"]) for r in llm)
    for r in realtime:
        input_tokens += int(r["input_text_tokens"]) + int(r["input_audio_tokens"])
        cached_input_tokens += int(r["input_cached_text_tokens"]) + int(
            r["input_cached_audio_tokens"]
        )
        output_tokens += int(r["output_text_tokens"]) + int(r["output_audio_tokens"])

    # audio_duration is provider-reported seconds; the UI works in ms.
    stt_audio_ms = round(sum(float(r["audio_duration"]) for r in stt) * 1000)
    tts_audio_ms = round(sum(float(r["audio_duration"]) for r in tts) * 1000)

    providers = sorted(
        {str(row["provider"]) for rows in usage_rows.values() for row in rows if row["provider"]}
    )
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "stt_audio_ms": stt_audio_ms,
        "tts_audio_ms": tts_audio_ms,
        "total_audio_ms": stt_audio_ms + tts_audio_ms,
        "tts_characters": sum(int(r["characters_count"]) for r in tts),
        "providers": providers,
    }


def build_health_snapshot(
    run: RunFacts,
    transcript: Sequence[asyncpg.Record],
    events: Sequence[asyncpg.Record],
    usage_rows: dict[str, Sequence[asyncpg.Record]],
) -> HealthSnapshotResponse:
    noun = run.noun
    messages = [row for row in transcript if row["type"] == "message"]
    user_messages = [row for row in messages if row["role"] == "user"]
    agent_messages = [row for row in messages if row["role"] == "assistant"]
    interruptions = sum(1 for row in agent_messages if _is_interrupted(row))

    # The same breakdowns the transcript draws per reply, so the band's numbers
    # and the bars below it cannot disagree. A response is the caller stopping →
    # agent audio; on text, the message arriving → the first token back.
    waits = reply_breakdowns(transcript, events).values()
    responses = [wait for wait in waits if wait.kind in ("response", "reply")]
    response_latency = latency_stats([wait.total_ms for wait in responses])
    slow_after_filler = sum(
        1 for wait in waits if wait.kind == "after_filler" and wait.total_ms > SLOW_RESPONSE_MS
    )

    tools = _tool_facts(transcript, events, messages)

    error_events = [event for event in events if event["type"] == SESSION_ERROR]
    errors = len(error_events)
    provider_failures = sum(1 for event in events if event["type"] == PROVIDER_FAILED)
    provider_recoveries = sum(1 for event in events if event["type"] == PROVIDER_RECOVERED)
    agent_became_ready = any(event["type"] == AGENT_READY for event in events)
    connection = _connection_quality(events)
    false_interruptions = [e for e in events if e["type"] == AGENT_FALSE_INTERRUPTION]
    noise_cancellation_failed = any(e["type"] == NOISE_CANCELLATION_FAILED for e in events)

    totals = _usage_totals(usage_rows)

    # (kind, sentence): the kind is what lets a reader find the moment.
    issues: list[tuple[IssueKind, str]] = []
    if run.failed:
        reason = (run.close_reason or "unknown reason").replace("_", " ")
        issues.append(("failed", f"The {noun} failed ({reason})"))
    # "Ended with nothing in it" is the signature of a start that never
    # happened — worth naming, because the transcript looks merely empty.
    if run.terminal and not agent_became_ready and not messages:
        issues.append(("never_ready", f"The agent never became ready before the {noun} ended"))
    if errors:
        # Which provider, because that is the whole actionable half: "5 provider
        # errors" sends a reader hunting through the trace for a name we already
        # have on every one of these events.
        issues.append(
            (
                "provider_errors",
                f"{_erroring_providers(error_events)} raised {_plural(errors, 'error')} "
                f"during the {noun}",
            )
        )
    if provider_failures:
        issues.append(
            (
                "provider_failover",
                f"{_plural(provider_failures, 'provider')} failed over to "
                f"{'its' if provider_failures == 1 else 'their'} backup"
                + (f", {provider_recoveries} recovered" if provider_recoveries else ""),
            )
        )
    if tools.failures:
        issues.append(("tool_failures", f"{_plural(tools.failures, 'tool call')} failed"))
    # Only once nothing more can arrive: a tool still running on a live call is
    # the ordinary state of every tool, not a finding.
    if run.terminal and tools.unfinished:
        issues.append(
            (
                "tools_unfinished",
                f"{_plural(tools.unfinished, 'tool call')} never finished — the {noun} ended "
                f"while {'it was' if tools.unfinished == 1 else 'they were'} still running",
            )
        )
    # Only the ones nothing was spoken over. A tool that took twelve seconds
    # while the agent read out a confirmation is the `say` operation working,
    # and calling that "the caller waiting in silence" accuses a builder of a
    # defect they deliberately designed.
    if tools.slow_in_silence:
        issues.append(
            (
                "slow_tools",
                f"{_plural(tools.slow_in_silence, 'tool call')} took longer than "
                f"{SLOW_TOOL_MS / 1000:.0f}s with nothing spoken, leaving the caller in silence",
            )
        )
    p95 = response_latency["p95_ms"]
    if p95 is not None and p95 > SLOW_RESPONSE_MS:
        issues.append(("slow_responses", _slow_response_issue(responses, float(p95))))
    if slow_after_filler:
        issues.append(
            (
                "slow_after_filler",
                f"{slow_after_filler} {'reply' if slow_after_filler == 1 else 'replies'} left the "
                f"caller in silence for over {SLOW_RESPONSE_MS / 1000:g}s after a filler line",
            )
        )
    # Whose network it was, when the trace says. The flat sentence survives only
    # for calls recorded before the side was captured.
    for level, wording in (("poor", "dropped to poor"), ("lost", "was lost")):
        sides = [side for side in ("agent", "caller") if getattr(connection, side) == level]
        if sides:
            whose = " and ".join("our side" if s == "agent" else "the caller's side" for s in sides)
            issues.append(("connection", f"Connection quality {wording} on {whose}"))
            break
        if connection.overall == level:
            issues.append(
                (
                    "connection",
                    f"Connection quality {wording} during the call (which side was not recorded)",
                )
            )
            break
    # Textbook platform malfunction: the enhancer turned itself off and the agent
    # editor is still showing the toggle on.
    if noise_cancellation_failed:
        issues.append(
            ("noise_cancellation", "Noise cancellation failed and the call ran on raw audio")
        )
    if run.billing_status == "unpriceable":
        issues.append(
            ("unpriceable", f"This {noun} could not be priced — check the provider/model catalog")
        )

    return HealthSnapshotResponse(
        duration_seconds=run.duration_seconds,
        turns=len(user_messages),
        user_messages=len(user_messages),
        agent_messages=len(agent_messages),
        interruptions=interruptions,
        uninterrupted_responses=len(agent_messages) - interruptions,
        pace=_pace(messages, events),
        response_latency=LatencyStats.model_validate(response_latency),
        response_stages=_average_stages(responses),
        total_cost_usd=run.total_cost_usd,
        billing_status=run.billing_status,
        tool_calls=tools.total,
        tool_call_failures=tools.failures,
        unfinished_tool_calls=tools.unfinished,
        slow_tool_calls=tools.slow,
        slow_tool_calls_in_silence=tools.slow_in_silence,
        slow_replies_after_filler=slow_after_filler,
        errors=errors,
        provider_failures=provider_failures,
        provider_recoveries=provider_recoveries,
        agent_became_ready=agent_became_ready,
        connection_quality=connection.overall,
        connection=connection,
        false_interruptions=len(false_interruptions),
        false_interruptions_resumed=sum(
            1 for e in false_interruptions if e["payload"].get("resumed")
        ),
        noise_cancellation_failed=noise_cancellation_failed,
        ok=not issues,
        issues=[HealthIssue(kind=kind, message=message) for kind, message in issues],
        **totals,
    )
