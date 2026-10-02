"""Private helpers for session persistence."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from livekit.agents import llm

from services.tools import HandoffTarget, ToolDefinition
from workers.voice.keypad import ENTRY_PREFIX as KEYPAD_ENTRY_PREFIX

logger = logging.getLogger("talqing.workers.session")


def _jsonable_str(value: object, *, field: str) -> str:
    """Coerce stored tool args/output into the string LiveKit expects."""
    if isinstance(value, str):
        return value
    if value is None:
        raise ValueError(f"{field} is missing")
    return json.dumps(value, default=str)


def _string_list(value: object) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("expected a list of strings")
    return [str(item) for item in value]


# What each activity switch reads as. LiveKit writes the same `AgentHandoff`
# item for all three, so the map `build_handoff_agent` / `build_task_agent`
# recorded is the only thing that can tell them apart.
_HANDOFF_TEXT = {
    "handoff": "Handed off to {name}",
    "task": "Started {name}",
    "task_return": "Back to {name}",
}


@dataclass(frozen=True, slots=True)
class ItemRow:
    """One LiveKit chat item as the `conversation_items` columns it is stored in.

    Built once for both workers, so a call and a chat cannot describe the same
    item differently. What stays with each writer is what genuinely differs
    between them: the agent stamp, and how a text plane delivers a reply.
    """

    livekit_item_id: str
    type: str
    role: str
    text: str | None
    direction: str
    visibility: str
    # LiveKit's MetricsReport for a message, in its own column.
    metrics: dict[str, object] | None
    # `metadata.data`: the LiveKit extras for the item's type.
    data: dict[str, object | None]
    # The item's OWN timestamp, not the moment it is persisted. Every ChatItem
    # carries one, and for a function call LiveKit resets it to when the tool
    # started — so a tool that runs for thirty seconds still sorts before the
    # reply it produced, and a task's entry call before everything said inside it.
    created_at: datetime
    # Who an `agent_handoff` switched to, when this run built the target.
    handoff: HandoffTarget | None


def item_row(item: llm.ChatItem, handoff_targets: Mapping[str, HandoffTarget]) -> ItemRow | None:
    """Serialize a LiveKit chat item. None for a type this table does not hold."""
    row = {
        "livekit_item_id": item.id,
        "type": item.type,
        "role": "tool",
        "text": None,
        "direction": "internal",
        "visibility": "internal",
        "metrics": None,
        "created_at": datetime.fromtimestamp(item.created_at, UTC),
        "handoff": None,
    }
    if item.type == "message":
        data: dict[str, object | None] = {"interrupted": item.interrupted}
        if item.role == "user":
            # How sure the STT was of this turn: the direct explanation for a
            # mis-heard word. LiveKit sets it on every transcribed turn (1.0 when
            # the provider did not score it) and on nothing else, which makes its
            # absence the one honest sign that a caller message was not spoken.
            if item.transcript_confidence is not None:
                data["transcript_confidence"] = float(item.transcript_confidence)
                data["origin"] = "speech"
            else:
                keyed = (item.text_content or "").startswith(KEYPAD_ENTRY_PREFIX)
                data["origin"] = "keypad" if keyed else "typed"
        spoken = item.role in ("user", "assistant")
        return ItemRow(
            **row
            | {
                "role": item.role,
                "text": item.text_content or None,
                "direction": {"user": "inbound", "assistant": "outbound"}.get(
                    item.role, "internal"
                ),
                "visibility": "customer_visible" if spoken else "internal",
                "metrics": dict(item.metrics) if item.metrics else None,
            },
            data=data,
        )
    if item.type == "function_call":
        data = {"name": item.name, "call_id": item.call_id, "arguments": item.arguments}
        if item.group_id is not None:
            data["group_id"] = item.group_id
        return ItemRow(**row, data=data)
    if item.type == "function_call_output":
        return ItemRow(
            **row,
            data={
                "name": item.name,
                "call_id": item.call_id,
                "output": item.output,
                "is_error": item.is_error,
            },
        )
    if item.type == "agent_handoff":
        data = {"old_agent_id": item.old_agent_id, "new_agent_id": item.new_agent_id}
        # The item carries only LiveKit agent ids, and a task's or a team
        # member's names no `agents` row — so who the switch was TO is written
        # here, from the map the run recorded as each target was built. Without a
        # target this is the row LiveKit writes when the entry agent takes the
        # session, which nobody on the run built.
        target = handoff_targets.get(item.new_agent_id)
        text = "Agent handoff"
        if target is not None:
            text = _HANDOFF_TEXT[target.kind].format(name=target.name)
            data["kind"] = target.kind
            # What pairs a task's "Started" row with its "Back to" row, and both
            # with the tool call that entered it.
            if target.entry_call_id is not None:
                data["entry_call_id"] = target.entry_call_id
        return ItemRow(**row | {"role": "system", "text": text, "handoff": target}, data=data)
    if item.type == "agent_config_update":
        return ItemRow(
            **row | {"role": "system"},
            data={
                "instructions": item.instructions,
                "tools_added": item.tools_added,
                "tools_removed": item.tools_removed,
            },
        )
    return None


def _frozen_tool_definitions(value: object) -> list[ToolDefinition]:
    """Decode tool_versions.definition JSONB. Fail on corrupt storage."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError("frozen tool definitions must be a JSON array")
    out: list[ToolDefinition] = []
    for item in value:
        if not isinstance(item, dict):
            raise TypeError("each frozen tool definition must be a JSON object")
        for required in (
            "id",
            "name",
            "description",
            "json_schema",
            "long_running_task",
            "silent",
            "disable_interruptions",
            "operations",
        ):
            if required not in item:
                raise ValueError(f"frozen tool definition missing required field {required!r}")
        out.append(item)  # type: ignore[arg-type]
    return out
