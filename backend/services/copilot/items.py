"""Project CoPilot conversation_items rows to dashboard shape."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from api.dataplane.functions import DEFERRAL_FUNCTIONS

from .models import (
    CopilotFunctionCallItem,
    CopilotFunctionCallOutputItem,
    CopilotTextItem,
    PublicMessage,
)


def hidden_from_rail(row: Mapping[str, Any]) -> bool:
    """True for a timeline row the editor rail must not show.

    Every other call a CoPilot makes is something that happened to the user's
    workspace, which is why the rail lists them under their real API names. A
    `describe_function` / `describe_schema` call is not: it is the model
    fetching an argument shape we left out of its tool definitions to keep the
    whole surface inside a context window. Putting that on the rail spends the
    reader's attention on an internal optimisation they can neither act on nor
    recognise — so it stays in `conversation_items`, where the model's own
    history needs it, and out of everything the user sees.

    Both halves of the call go: LiveKit stamps the function's name on the output
    as well as on the call.

    A malformed row is left for `public_message` to reject rather than quietly
    dropped here.
    """
    if row["type"] not in ("function_call", "function_call_output"):
        return False
    metadata = row["metadata"]
    data = metadata.get("data") if isinstance(metadata, dict) else None
    return isinstance(data, dict) and data.get("name") in DEFERRAL_FUNCTIONS


def public_message(row: Mapping[str, Any]) -> PublicMessage:
    """Map one timeline row to the editor SSE/message payload.

    Keeps the previous Responses-shaped `item` field so the dashboard can keep
    rendering user/assistant bubbles and action chips without a redesign.
    """
    item_type = row["type"]
    role = row["role"]
    text = row["text"]
    metadata = row["metadata"]
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise TypeError("conversation item metadata must be a JSON object")
    data = metadata.get("data")
    if data is None:
        data = {}
    elif not isinstance(data, dict):
        raise TypeError("conversation item metadata.data must be a JSON object")
    created_at = row["created_at"]
    if isinstance(created_at, datetime):
        created = created_at.isoformat()
    else:
        raise TypeError(
            f"conversation item created_at must be datetime, got {type(created_at).__name__}"
        )
    item_id = str(row["id"])

    if item_type == "message":
        if role is None:
            raise ValueError("message item is missing role")
        if text is None:
            raise ValueError("message item is missing text")
        return PublicMessage(
            id=item_id,
            item=CopilotTextItem(role=role, content=text),
            role=role,
            content=text,
            created_at=created,
        )

    if item_type == "function_call":
        call_id = data.get("call_id")
        name = data.get("name")
        if not isinstance(call_id, str) or not call_id:
            raise ValueError("function_call item is missing call_id")
        if not isinstance(name, str) or not name:
            raise ValueError("function_call item is missing name")
        arguments = data.get("arguments")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments if arguments is not None else {}, default=str)
        return PublicMessage(
            id=item_id,
            item=CopilotFunctionCallItem(call_id=call_id, name=name, arguments=arguments),
            role="assistant",
            tool_calls=[{"id": call_id, "name": name, "arguments": arguments}],
            name=name,
            created_at=created,
        )

    if item_type == "function_call_output":
        call_id = data.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            raise ValueError("function_call_output item is missing call_id")
        output = data.get("output")
        if not isinstance(output, str):
            output = json.dumps(output if output is not None else {}, default=str)
        result_name = data.get("name")
        return PublicMessage(
            id=item_id,
            item=CopilotFunctionCallOutputItem(call_id=call_id, output=output),
            role="tool",
            content=output,
            tool_call_id=call_id,
            name=result_name if isinstance(result_name, str) and result_name else None,
            created_at=created,
        )

    # agent_handoff / agent_config_update / system failure messages
    content = text if isinstance(text, str) and text else "Something went wrong."
    return PublicMessage(
        id=item_id,
        item=CopilotTextItem(role=role or "system", content=content),
        role=role or "system",
        content=content,
        created_at=created,
    )


def as_uuid(value: UUID | str) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))
