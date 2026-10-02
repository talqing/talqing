"""Bounded client→agent userdata RPC for web-call SDK participants."""

from __future__ import annotations

import json

from livekit import rtc
from livekit.agents import AgentSession, JobContext

from services.tools import SessionUserData
from services.userdata import RESERVED_KEY_ERROR, is_reserved_key

CLIENT_USERDATA_RPC_MAX_BYTES = 16 * 1024


def register_client_handlers(
    ctx: JobContext,
    session: AgentSession[SessionUserData],
    participant_identity: str | None,
) -> None:
    """Register ``talqing.client.userdata_{get,set}`` on the local participant.

    The client is untrusted — these never expose secrets (userdata holds none).
    """

    def _userdata(s: AgentSession[SessionUserData]) -> SessionUserData:
        return s.userdata if isinstance(s.userdata, dict) else {}

    def _is_authorized(data: rtc.RpcInvocationData) -> bool:
        return participant_identity is None or data.caller_identity == participant_identity

    def _payload_size(data: rtc.RpcInvocationData) -> int:
        return len((data.payload or "").encode("utf-8"))

    def _public_userdata() -> dict[str, object]:
        return {
            k: v
            for k, v in _userdata(session).items()
            if isinstance(k, str) and not is_reserved_key(k)
        }

    async def _set(data: rtc.RpcInvocationData) -> str:
        if not _is_authorized(data):
            return json.dumps({"ok": False, "error": "unauthorized caller"})
        if _payload_size(data) > CLIENT_USERDATA_RPC_MAX_BYTES:
            return json.dumps({"ok": False, "error": "payload too large"})
        try:
            patch = json.loads(data.payload or "{}")
        except json.JSONDecodeError:
            return json.dumps({"ok": False, "error": "payload must be a JSON object"})
        if not isinstance(patch, dict):
            return json.dumps({"ok": False, "error": "payload must be a JSON object"})
        if any(not isinstance(k, str) or not k for k in patch):
            return json.dumps({"ok": False, "error": "userdata keys must be non-empty strings"})
        if any(is_reserved_key(k) for k in patch):
            return json.dumps({"ok": False, "error": RESERVED_KEY_ERROR})
        _userdata(session).update(patch)
        return json.dumps({"ok": True})

    async def _get(data: rtc.RpcInvocationData) -> str:
        if not _is_authorized(data):
            return json.dumps({"ok": False, "error": "unauthorized caller"})
        if _payload_size(data) > CLIENT_USERDATA_RPC_MAX_BYTES:
            return json.dumps({"ok": False, "error": "payload too large"})
        try:
            body = json.loads(data.payload or "{}")
        except json.JSONDecodeError:
            body = {}
        keys = body.get("keys") if isinstance(body, dict) else None
        ud = _public_userdata()
        if isinstance(keys, list):
            ud = {k: ud.get(k) for k in keys if isinstance(k, str) and not is_reserved_key(k)}
        return json.dumps(ud, default=str)

    ctx.room.local_participant.register_rpc_method("talqing.client.userdata_set", _set)
    ctx.room.local_participant.register_rpc_method("talqing.client.userdata_get", _get)
