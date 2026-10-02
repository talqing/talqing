"""Turning a finished call into one LLM request, and the reply back into values.

The whole analysis is a single request. Composing one schema from the summary,
the outcome and every field costs one copy of the transcript; asking per field
would resend it once each, and the transcript is nearly all of the input.

The reply is requested as a plain JSON object with the schema spelled out in
the prompt, rather than through a provider's `json_schema` response format.
Talqing's catalog spans several vendors behind one OpenAI-compatible surface,
and strict schema support is not something every one of them offers — a mode
that silently degrades on one provider would give that provider's tenants
quietly worse extractions.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from services.agents.models import AnalysisSpec

# Keys the built-in outputs occupy in the reply object. `_RESERVED_ANALYSIS_NAMES`
# on the spec side keeps tenant fields from colliding with them.
SUMMARY_KEY = "summary"
OUTCOME_KEY = "outcome"
RATIONALE_KEY = "outcome_rationale"

_SYSTEM = (
    "You are analysing a finished conversation between an AI phone agent and a person. "
    "You will be given the agent's own instructions and the full transcript, including the "
    "tools the agent called and what they returned.\n\n"
    "Report only what the transcript supports. Never infer a value the conversation does not "
    "contain, and never carry over an example from these instructions. When something was not "
    "established, use null.\n\n"
    "Reply with a single JSON object and nothing else, matching this schema:\n"
    "{schema}"
)


def _outcome_property(prompt: str) -> dict[str, Any]:
    return {
        "type": "string",
        "enum": ["success", "failure", "unknown"],
        "description": (
            "Did the call achieve its purpose? The agent's author defines success as: "
            f"{prompt}\n"
            "Use 'unknown' when the transcript genuinely does not settle it — a call that "
            "ended too early to tell is unknown, not a failure."
        ),
    }


def reply_schema(spec: AnalysisSpec) -> dict[str, Any]:
    """The JSON schema for everything this spec asks for, in one object."""
    properties: dict[str, Any] = {}
    if spec.summary:
        properties[SUMMARY_KEY] = {
            "type": "string",
            "description": (
                "What happened, in two or three sentences: why the person called, what the "
                "agent did, and how it ended. Plain prose, no preamble."
            ),
        }
    if spec.outcome is not None:
        properties[OUTCOME_KEY] = _outcome_property(spec.outcome.prompt)
        properties[RATIONALE_KEY] = {
            "type": "string",
            "description": (
                "One or two sentences citing what in the transcript decided the outcome."
            ),
        }
    for field in spec.fields:
        properties[field.name] = {
            # Nullable throughout: a call that never mentions the value must be
            # able to say so. A required scalar would push the model into
            # inventing one, which is the failure mode that matters here.
            "type": [field.type, "null"],
            "description": field.description,
        }
    return {"type": "object", "properties": properties, "required": list(properties)}


def _speaker(role: str | None) -> str:
    # "agent"/"caller" rather than "assistant"/"user": the model is reading a
    # phone call, and the transcript should read like one.
    return {"assistant": "agent", "user": "caller"}.get(role or "", role or "unknown")


def _attachment_note(item: dict[str, object]) -> str:
    """How an image reads in a transcript that is only text.

    Named rather than counted: "sent an image: cracked-screen.jpg" is something
    the model can reason about, where "[1 image]" only says that something was
    missing from what it was given. The pixels are deliberately not sent — that
    is a separate decision with its own cost.
    """
    attachments = item.get("attachments")
    if not isinstance(attachments, list) or not attachments:
        return ""
    names = [str(a.get("filename") or "image") for a in attachments if isinstance(a, dict)]
    return (
        f"[sent an image: {names[0]}]" if len(names) == 1 else f"[sent images: {', '.join(names)}]"
    )


def render_transcript(transcript: list[dict[str, object]]) -> str:
    """One line per item, tool calls included.

    Tool activity is not decoration here — whether `book_appointment` returned
    an error is routinely the single most informative line in the call, and an
    outcome judged without it would be guessing from small talk.
    """
    lines: list[str] = []
    for item in transcript:
        data = item.get("data")
        data = data if isinstance(data, dict) else {}
        kind = item.get("type")
        if kind == "message":
            text = str(data.get("text") or "").strip()
            note = _attachment_note(item)
            if not text and not note:
                continue
            said = f"{note} {text}".strip() if note else text
            interrupted = " [interrupted]" if data.get("interrupted") else ""
            lines.append(f"{_speaker(data.get('role'))}: {said}{interrupted}")
        elif kind == "function_call":
            lines.append(f"[tool call: {data.get('name')}({data.get('arguments')})]")
        elif kind == "function_call_output":
            label = "tool error" if data.get("is_error") else "tool result"
            lines.append(f"[{label}: {data.get('name')} → {data.get('output')}]")
        elif kind == "agent_handoff":
            lines.append("[handed off to another agent]")
    return "\n".join(lines)


def user_turn_count(transcript: list[dict[str, object]]) -> int:
    """Caller turns that carried something — the gate's unit.

    An image counts. A call made entirely of photos is a call where the caller
    said plenty; judging it as one where nobody spoke, and refusing to analyse
    it for being too short, would be exactly backwards.
    """
    count = 0
    for item in transcript:
        if item.get("type") != "message":
            continue
        data = item.get("data")
        data = data if isinstance(data, dict) else {}
        if data.get("role") != "user":
            continue
        if str(data.get("text") or "").strip() or _attachment_note(item):
            count += 1
    return count


def transfer_note(transfer: Mapping[str, object] | None) -> str:
    """Why the transcript stops where it does, when it stops at a human.

    Without this the model reads a call that ends mid-conversation and writes a
    summary that trails off — or calls a successful escalation a failure. The
    conversation the caller then had with a person is not recorded and is not
    the model's to guess at, so say that too.
    """
    if not transfer:
        return ""
    destination = str(transfer.get("destination") or "another number")
    # Warm mode ran a whole conversation the transcript does not contain: our
    # agent briefed the person answering, on a separate line, while the caller
    # was on hold. Saying so is what stops the model reading the gap as dead air.
    warm = transfer.get("mode") == "warm"
    if transfer.get("outcome") == "connected":
        briefed = (
            " Before handing over, the agent spoke to that person privately and briefed them on "
            "the call; the caller was on hold and did not hear it, and it is not in the "
            "transcript."
            if warm
            else ""
        )
        return (
            f"\n\n---\n\nThe agent transferred this caller to a person on {destination}, and the "
            f"transfer connected.{briefed} The transcript ends there because the agent left the "
            "call. What the caller and that person then said is not recorded — do not invent it. "
            "Treat reaching a person as what happened, not as the call being cut short."
        )
    # The reason, not a guess at one: "could not reach them" is wrong for a
    # person who answered and declined, which only warm mode can tell apart.
    detail = str(transfer.get("detail") or "").strip()
    because = f" but could not: {detail}" if detail else " and could not reach them"
    return (
        f"\n\n---\n\nThe agent tried to transfer this caller to a person on {destination}"
        f"{because}. The call carried on with the agent."
    )


def build_messages(
    spec: AnalysisSpec,
    *,
    transcript: list[dict[str, object]],
    system_prompt: str,
    close_reason: str | None,
    transfer: Mapping[str, object] | None = None,
) -> list[dict[str, str]]:
    schema = json.dumps(reply_schema(spec), indent=2)
    rendered = render_transcript(transcript)
    return [
        {"role": "system", "content": _SYSTEM.format(schema=schema)},
        {
            "role": "user",
            "content": (
                f"The agent was given these instructions:\n\n{system_prompt}\n\n"
                f"---\n\nTranscript:\n\n{rendered}\n\n"
                f"---\n\nThe call ended because: {close_reason or 'the call completed normally'}"
                f"{transfer_note(transfer)}"
            ),
        },
    ]


def _coerce(value: object, field_type: str) -> object | None:
    """One extracted value, as the declared type — or null.

    Null rather than an error: a field the conversation never established is
    the normal case, and one unparseable value must not cost the reader the
    summary and the other twenty fields.
    """
    if value is None or value == "":
        return None
    if field_type == "string":
        return str(value)
    if field_type == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("true", "yes"):
                return True
            if lowered in ("false", "no"):
                return False
        return None
    try:
        # bool is an int subclass, and True would otherwise silently meter as 1.
        if isinstance(value, bool):
            return None
        return int(value) if field_type == "integer" else float(value)
    except (TypeError, ValueError):
        return None


def parse_reply(spec: AnalysisSpec, reply: object) -> dict[str, Any]:
    """Pull the configured outputs out of a parsed reply object.

    Returns ``{"summary", "outcome", "outcome_rationale", "fields"}``. Anything
    the model omitted, or answered in a shape the field cannot hold, comes back
    as None — see ``_coerce``.
    """
    obj = reply if isinstance(reply, dict) else {}

    summary = obj.get(SUMMARY_KEY) if spec.summary else None
    summary = str(summary).strip() if isinstance(summary, str) and summary.strip() else None

    outcome = None
    rationale = None
    if spec.outcome is not None:
        raw = obj.get(OUTCOME_KEY)
        if isinstance(raw, str) and raw.strip().lower() in ("success", "failure", "unknown"):
            outcome = raw.strip().lower()
        else:
            # The judge answered with something that is not a verdict. That is
            # itself an uncertain verdict, not a missing one.
            outcome = "unknown"
        raw_rationale = obj.get(RATIONALE_KEY)
        if isinstance(raw_rationale, str) and raw_rationale.strip():
            rationale = raw_rationale.strip()

    fields = {f.name: _coerce(obj.get(f.name), f.type) for f in spec.fields}
    return {
        "summary": summary,
        "outcome": outcome,
        "outcome_rationale": rationale,
        "fields": fields,
    }
