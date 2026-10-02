"""CoPilot service — the platform's builder chats.

A CoPilot is a code-defined text agent that edits one resource in the editor the
user has open: **AgentCoPilot** an agent, **ToolCoPilot** a tool,
**TaskCoPilot** an agent task. They share
storage, transport, prompting machinery and the turn loop; what differs is a
``CopilotSubject``.

Import what you need from here::

    from services.copilot import AGENT_COPILOT, TOOL_COPILOT, send_message
    from services import copilot
    await copilot.send_message(copilot.TOOL_COPILOT, tool_id, body, ctx)

Submodules are package-internal; external callers should not import them.

Each CoPilot binds its slice of the exposed API functions
(``api.dataplane.functions``) through ``build_copilot_functions``, plus web
access. All of them run on ``CopilotAgent``.
"""

from __future__ import annotations

from .agent import CopilotAgent  # noqa: F401
from .const import COPILOT_MAX_STEPS  # noqa: F401
from .functions import build_copilot_functions  # noqa: F401
from .items import as_uuid, hidden_from_rail, public_message  # noqa: F401
from .models import (  # noqa: F401
    CopilotEvent,
    CopilotFunctionCallItem,
    CopilotFunctionCallOutputItem,
    CopilotItem,
    CopilotMessageEvent,
    CopilotSnapshotEvent,
    CopilotTextItem,
    PublicMessage,
    SendMessageRequest,
    SendMessageResponse,
    SnapshotPayload,
)
from .service import get_snapshot, send_message  # noqa: F401
from .stream import channel, get_redis, publish  # noqa: F401
from .subjects import (  # noqa: F401
    AGENT_COPILOT,
    SUBJECTS,
    SUBJECTS_BY_SENTINEL,
    TASK_COPILOT,
    TOOL_COPILOT,
    CopilotKind,
    CopilotSubject,
)
