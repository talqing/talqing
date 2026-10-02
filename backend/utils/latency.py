"""Shared conversational latency sampling (call detail + workspace observability).

LiveKit stores per-turn MetricsReport fields in SECONDS on chat messages:
- STT / endpointing on user messages (transcription_delay, end_of_turn_delay)
- LLM / TTS / E2E on assistant messages (llm_node_ttft, tts_node_ttfb, e2e_latency)

Voice typically reports the full set. Text usually only has llm_node_ttft (no
synthesized e2e — that field is speech-based in LiveKit).

Both call detail and workspace observability use the same rule: for each metric,
average every message that carries a non-negative sample. No user↔assistant
pairing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from services.session_events import TOOL_ENDED

LatencyField = Literal[
    "llm_node_ttft",
    "tts_node_ttfb",
    "transcription_delay",
    "e2e_latency",
    "end_of_turn_delay",
]

# Match call-detail + observability cards. Hook/playback metrics are MVP-out.
LATENCY_FIELDS: tuple[LatencyField, ...] = (
    "e2e_latency",
    "transcription_delay",
    "end_of_turn_delay",
    "llm_node_ttft",
    "tts_node_ttfb",
)


def _metric_sample(metrics: Mapping[str, Any], key: str) -> float | None:
    """Return a non-negative sample in seconds, or None if missing/invalid."""
    value = metrics.get(key)
    # 0.0 is a real measurement for some fields; negative sentinels (e.g. -1)
    # and non-numeric values are not samples.
    return float(value) if isinstance(value, (int, float)) and value >= 0 else None


def _columns(row: Any) -> frozenset[str]:
    """The row's column names, as a set that survives being tested twice.

    **`asyncpg.Record.keys()` is a one-shot iterator, not a view like a dict's.**
    So `"a" in keys` consumes it up to the match, and a second test for a column
    earlier in the SELECT reads as absent though the value is right there. That
    is not hypothetical — it is why `ConversationResponse.contact_key` was null
    on every endpoint until 2026-08-18. Anything without columns has none.
    """
    keys = getattr(row, "keys", None)
    if keys is not None:
        return frozenset(keys())
    if isinstance(row, Mapping):
        return frozenset(row)
    return frozenset()


def _message_metrics(row: Any) -> dict[str, Any] | None:
    """Extract MetricsReport samples from a row's first-class ``metrics`` field.

    Rows are DB records (conversation_items.metrics) or in-memory transcript
    dicts with the same top-level key — never nested under data/metadata. The
    producers differ in which columns they select, so unlike a row mapper bound
    to one SELECT this genuinely has to ask what it was given.
    """
    columns = _columns(row)
    row_type = row["type"] if "type" in columns else None
    if row_type is not None and row_type != "message":
        return None

    if "metrics" not in columns:
        return None
    metrics = row["metrics"]
    if not isinstance(metrics, dict) or not metrics:
        return None
    return metrics


def latency_samples(transcript_rows: Iterable[Any], field: LatencyField) -> list[float]:
    """Every non-negative sample of one field, in MILLISECONDS, in turn order.

    ``compute_latency`` collapses to a mean; the call snapshot also needs the
    distribution (a p95 is what tells you whether a call was *sometimes* slow).
    """
    samples: list[float] = []
    for row in transcript_rows:
        metrics = _message_metrics(row)
        if metrics is None:
            continue
        if (sample := _metric_sample(metrics, field)) is not None:
            samples.append(round(sample * 1000.0, 1))
    return samples


def latency_stats(samples: list[float]) -> dict[str, float | int | None]:
    """Mean / p95 / max over already-converted millisecond samples."""
    if not samples:
        return {"avg_ms": None, "p95_ms": None, "max_ms": None, "samples": 0}
    ordered = sorted(samples)
    # Nearest-rank on a zero-based list: with few turns this lands on the
    # slowest sample, which is the honest reading of "p95 of 3 turns".
    p95_index = round(0.95 * (len(ordered) - 1))
    return {
        "avg_ms": round(sum(ordered) / len(ordered), 1),
        "p95_ms": ordered[p95_index],
        "max_ms": ordered[-1],
        "samples": len(ordered),
    }


def compute_latency(
    transcript_rows: Iterable[Any],
) -> dict[str, float | int | None]:
    """Average each latency field over every message that reports it.

    Values are converted from seconds → milliseconds. Returns keys in
    LATENCY_FIELDS plus ``turns`` (count of messages with a valid e2e_latency
    sample — the natural "responded turns" denominator).
    """
    sums: dict[str, float] = {key: 0.0 for key in LATENCY_FIELDS}
    counts: dict[str, int] = {key: 0 for key in LATENCY_FIELDS}

    for row in transcript_rows:
        metrics = _message_metrics(row)
        if metrics is None:
            continue
        for key in LATENCY_FIELDS:
            if (sample := _metric_sample(metrics, key)) is not None:
                sums[key] += sample
                counts[key] += 1

    result: dict[str, float | int | None] = {
        key: round((sums[key] / counts[key]) * 1000.0, 1) if counts[key] else None
        for key in LATENCY_FIELDS
    }
    result["turns"] = counts["e2e_latency"]
    return result


# ── where one wait went ─────────────────────────────────────────────────────

StageKind = Literal["stt", "endpoint", "hook", "llm_step", "tool", "llm", "tts", "other"]

# The order the legs of a wait happen in, which is also the order a bar draws them.
STAGE_ORDER: tuple[StageKind, ...] = (
    "stt",
    "endpoint",
    "hook",
    "llm_step",
    "tool",
    "llm",
    "tts",
    "other",
)


class LatencyStage(BaseModel):
    """One leg of a wait: what the time went on."""

    kind: StageKind
    ms: float
    label: str


class ReplyLatency(BaseModel):
    """How long the caller waited for this reply, in the order it was spent."""

    # `response`: caller stopped speaking → first agent audio. `reply`: a typed
    # message received → the first token back. `after_filler`: the gap between a
    # line like "let me check" and the reply a tool produced.
    kind: Literal["response", "reply", "after_filler"]
    total_ms: float
    stages: list[LatencyStage]


def _unix(value: datetime) -> float:
    return value.timestamp()


def _seconds(metrics: Mapping[str, Any], key: str) -> float | None:
    """A positive duration in seconds. Zero is LiveKit's "not measured" here."""
    sample = _metric_sample(metrics, key)
    return sample if sample else None


def _ms(seconds: float) -> float:
    return round(max(seconds, 0.0) * 1000.0, 1)


def item_data(row: Any) -> Mapping[str, Any]:
    """LiveKit's own fields for one item: `call_id`, `interrupted`, `origin`…"""
    return (row["metadata"] or {}).get("data") or {}


def reply_breakdowns(items: Sequence[Any], events: Sequence[Any]) -> dict[Any, ReplyLatency]:
    """Every caller-perceived wait in a transcript, keyed by the item it ended on.

    ``items`` are `conversation_items` rows in stored order carrying `id`,
    `type`, `role`, `created_at`, `metrics` and `metadata`; ``events`` the
    session trace.

    Three kinds of wait, all measured from timestamps already stored:

    * **response** — the caller stopped, the agent spoke. The total is LiveKit's
      `e2e_latency`, untouched: it is what the caller sat through, tools
      included. LiveKit hands the caller's metrics to the FIRST thing spoken, so
      after a tool-only generation that is the tool's reply, whose
      `llm_node_ttft` covers the second generation only. The first generation
      and the tool are recovered here from when each tool started and ended.
    * **reply** — a message nobody spoke (a chat message, a keypad entry) was
      received, the agent began to answer. LiveKit measures `e2e_latency` from
      speech, so there is none: the wait runs from the message's `created_at` to
      the reply's first token. On text the first is the API host's clock and the
      second the worker's.
    * **after_filler** — the agent said something, a tool ran, the agent spoke
      again. That second message carries no `e2e_latency`, and the silence
      before it is one nothing else states.

    Stages are clamped at zero and never re-sorted, so a leg that overlapped
    another (preemptive generation, a tool that outlasted the line spoken over
    it) shrinks rather than going negative.
    """
    output_at: dict[str, float] = {}
    # When each task took the conversation over, by the call that entered it.
    # That is where the wait on the call ends: what follows is the task's own
    # model speaking, not a tool the caller is waiting on.
    task_began_at: dict[str, float] = {}
    for row in items:
        data = item_data(row)
        if row["type"] == "function_call_output":
            output_at[data["call_id"]] = _unix(row["created_at"])
        elif row["type"] == "agent_handoff" and data.get("kind") == "task":
            task_began_at[data["entry_call_id"]] = _unix(row["created_at"])
    # A background tool's `tool.ended` is when its WORK finished, which nobody
    # waited for: it handed the turn back at dispatch, and that is its output row.
    ended_at = {
        event["payload"]["call_id"]: _unix(event["created_at"])
        for event in events
        if event["type"] == TOOL_ENDED and not event["payload"].get("background")
    }

    out: dict[Any, ReplyLatency] = {}
    user: Mapping[str, Any] | None = None
    # When an unspoken message arrived, until something answers it.
    asked_at: float | None = None
    spoke_until: float | None = None
    # Tool rounds since the caller, or the agent, last spoke: [start, end, names].
    # Calls one generation made run together, so a call that starts before the
    # round it follows has ended belongs to that round.
    rounds: list[tuple[float, float | None, list[str]]] = []
    called: set[str] = set()

    for row in items:
        data = item_data(row)
        if row["type"] == "agent_handoff":
            # A task still running has no entry call stored yet (LiveKit emits it
            # when the task returns), so its start stands in for the round.
            entry = data.get("entry_call_id")
            if data.get("kind") == "task" and entry not in called:
                rounds.append((task_began_at[entry], task_began_at[entry], ["task"]))
            continue
        if row["type"] == "function_call":
            called.add(data["call_id"])
            start = _unix(row["created_at"])
            ends = [
                at
                for at in (output_at.get(data["call_id"]), ended_at.get(data["call_id"]))
                if at is not None
            ]
            end = task_began_at.get(data["call_id"], max(ends) if ends else None)
            if rounds and (rounds[-1][1] is None or start < rounds[-1][1]):
                first, last, names = rounds[-1]
                both = None if last is None or end is None else max(last, end)
                rounds[-1] = (first, both, [*names, data["name"]])
            else:
                rounds.append((start, end, [data["name"]]))
            continue
        if row["type"] != "message":
            continue
        metrics = row["metrics"] or {}
        if row["role"] == "user":
            user, spoke_until, rounds = metrics, None, []
            asked_at = (
                _unix(row["created_at"]) if data.get("origin") in ("typed", "keypad") else None
            )
            continue
        if row["role"] != "assistant":
            continue

        spoke_from = _metric_sample(metrics, "started_speaking_at")
        e2e = _metric_sample(metrics, "e2e_latency")
        stages: list[LatencyStage] = []
        kind: Literal["response", "reply", "after_filler"] | None = None
        total = 0.0
        # Where the stage being measured began. None when the transcript cannot
        # say (a realtime turn reports no end of caller speech), in which case
        # only the tool windows themselves are stated.
        boundary: float | None = None

        if e2e is not None and user is not None:
            kind, total = "response", e2e
            stt = _seconds(user, "transcription_delay")
            end_of_turn = _seconds(user, "end_of_turn_delay")
            hook = _seconds(user, "on_user_turn_completed_delay")
            if stt is not None:
                stages.append(LatencyStage(kind="stt", ms=_ms(stt), label="STT"))
            if end_of_turn is not None:
                # `end_of_turn_delay` includes transcription. With no separate
                # reading the whole wait is one unsplit leg, and says so.
                stages.append(
                    LatencyStage(
                        kind="endpoint",
                        ms=_ms(end_of_turn - (stt or 0.0)),
                        label="Endpoint" if stt is not None else "STT + endpoint",
                    )
                )
            if hook is not None:
                stages.append(LatencyStage(kind="hook", ms=_ms(hook), label="On user turn hook"))
            stopped = _metric_sample(user, "stopped_speaking_at")
            if stopped is not None:
                boundary = stopped + (end_of_turn or 0.0) + (hook or 0.0)
        elif e2e is None and asked_at is not None and spoke_from is not None:
            if spoke_from > asked_at:
                kind, total, boundary = "reply", spoke_from - asked_at, asked_at
        elif e2e is None and spoke_until is not None and spoke_from is not None and rounds:
            if spoke_from > spoke_until:
                kind, total, boundary = "after_filler", spoke_from - spoke_until, spoke_until
        asked_at = None

        if kind is not None:
            for index, (start, end, names) in enumerate(rounds):
                # A tool still running when the agent spoke was spoken INSIDE —
                # a task's opening line, a `say` in a tool tree — so the wait on
                # it ends at that first word.
                finish = min(end, spoke_from) if end is not None and spoke_from else end
                finish = finish if finish is not None else spoke_from
                if finish is None:
                    continue
                # The first round after a filler line was requested by the same
                # generation that spoke it, so no model call sits in front of it.
                if boundary is not None and not (kind == "after_filler" and index == 0):
                    stages.append(
                        LatencyStage(
                            kind="llm_step", ms=_ms(start - boundary), label="LLM → tool call"
                        )
                    )
                begin = start if boundary is None else max(start, boundary)
                stages.append(
                    LatencyStage(
                        kind="tool",
                        ms=_ms(finish - begin),
                        label=f"Tool {', '.join(dict.fromkeys(names))}",
                    )
                )
                boundary = finish if boundary is None else max(boundary, finish)
            llm = _seconds(metrics, "llm_node_ttft")
            tts = _seconds(metrics, "tts_node_ttfb")
            if llm is not None:
                stages.append(LatencyStage(kind="llm", ms=_ms(llm), label="LLM"))
            if tts is not None:
                stages.append(LatencyStage(kind="tts", ms=_ms(tts), label="TTS"))
            # What the stages above leave unexplained. Only stated beside a
            # measured model stage: a speech-to-speech turn reports none, and its
            # whole remainder would be the model itself wearing the wrong name.
            other = _ms(total) - sum(stage.ms for stage in stages)
            if llm is not None and other > 0:
                stages.append(LatencyStage(kind="other", ms=round(other, 1), label="Other"))
            out[row["id"]] = ReplyLatency(
                kind=kind, total_ms=_ms(total), stages=[s for s in stages if s.ms > 0]
            )

        stopped_speaking = _metric_sample(metrics, "stopped_speaking_at")
        if stopped_speaking is not None:
            spoke_until = stopped_speaking
        rounds = []
    return out
