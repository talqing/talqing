"""Gupshup's self-serve WhatsApp API (apps on apps.gupshup.io).

Auth is the app's API key in an ``apikey`` header. Every send also names the
app (``src.name``) and the sender number without its ``+``. Gupshup never signs
a webhook, so the URL carries a token (see ``webhook_token``).

- Session text: https://docs.gupshup.io/reference/session-text-message
- Templates: https://docs.gupshup.io/docs/template-messages
- Listing templates: https://docs.gupshup.io/reference/get-all-templates-for-an-app
- Inbound: https://docs.gupshup.io/docs/what-is-an-inbound-message
- Receipts: https://docs.gupshup.io/docs/message-events
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi import HTTPException

from .bsp import (
    BSP_TIMEOUT_SECONDS,
    BspError,
    FailureKind,
    InboundText,
    InboundUnsupported,
    MessageStatus,
    StatusEvent,
    Template,
    WebhookEvent,
    template_variables,
)

_API = "https://api.gupshup.io"

_EVENT_STATUS: dict[str, MessageStatus] = {
    "enqueued": "queued",
    "sent": "sent",
    "delivered": "delivered",
    "read": "read",
    "failed": "failed",
}

# Codes on an async `failed` event that are about the account or the template
# rather than the recipient. https://docs.gupshup.io/docs/message-events
_WALLET_CODES = {"1003"}
_TEMPLATE_CODES = {"4003", "4005", "132000", "132001", "132005", "132015", "132016"}
_ACCOUNT_CODES = {"1001", "1005", "1006", "131031"}
_RATE_LIMIT_CODES = {"4001", "130429", "131056"}
# Meta's per-person marketing limit ("healthy ecosystem engagement").
_HELD_BACK_CODES = {"131049"}
# Not on WhatsApp, or held back by Meta: see `twilio.EXPECTED_REFUSAL_CODES`.
EXPECTED_REFUSAL_CODES = frozenset({"1002", *_HELD_BACK_CODES})


# Inbound messages plus every receipt a batch records. Not `ALL`: in the v2
# format that delivers nothing (measured 2026-09-29).
_SUBSCRIPTION_MODES = "MESSAGE,ENQUEUED,SENT,DELIVERED,READ,FAILED"


def failure_kind(code: str | None) -> FailureKind:
    if code in _WALLET_CODES:
        return "wallet"
    if code in _TEMPLATE_CODES:
        return "template"
    if code in _ACCOUNT_CODES:
        return "account"
    if code in _RATE_LIMIT_CODES:
        return "rate_limited"
    if code in _HELD_BACK_CODES:
        return "held_back"
    return "row"


class GupshupWhatsApp:
    bsp = "gupshup"

    def __init__(self, *, api_key: str, app_id: str, app_name: str, sender_e164: str) -> None:
        self.api_key = api_key
        self.app_id = app_id
        self.app_name = app_name
        self.sender_e164 = sender_e164

    async def _request(
        self,
        method: str,
        path: str,
        action: str,
        *,
        data: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=BSP_TIMEOUT_SECONDS) as client:
                response = await client.request(
                    method,
                    f"{_API}{path}",
                    headers={"apikey": self.api_key},
                    data=data,
                    params=params,
                )
        except httpx.RequestError as exc:
            raise BspError(f"Gupshup {action} failed: {exc}", kind="transient") from exc
        try:
            body = response.json()
        except ValueError:
            body = {}
        message = str(body.get("message") or "") if isinstance(body, dict) else ""
        if response.status_code == 401:
            raise BspError(
                f"Gupshup rejected the API key while {action}; create a new one on the app's "
                "Settings → API Keys and update this integration",
                kind="account",
                code="401",
            )
        if response.status_code == 429:
            raise BspError(f"Gupshup rate limited {action}", kind="rate_limited", code="429")
        if response.status_code >= 500:
            raise BspError(
                f"Gupshup {action} failed (HTTP {response.status_code})", kind="transient"
            )
        if not response.is_success or not isinstance(body, dict) or body.get("status") == "error":
            detail = f"Gupshup {action} failed (HTTP {response.status_code})"
            if message:
                detail += f": {message}"
            lowered = message.lower()
            kind: FailureKind = "row"
            if "app" in lowered or "auth" in lowered:
                kind = "account"
            elif "template" in lowered:
                kind = "template"
            raise BspError(detail, kind=kind)
        return body

    def _number(self, e164: str) -> str:
        return e164.removeprefix("+")

    def peer_address(self, e164: str) -> str:
        """How a reply to ``e164`` is addressed: the number without its `+`."""
        return self._number(e164)

    async def list_templates(self) -> list[Template]:
        """Every approved template on the app; only text ones can be sent from a batch.

        Proves the app id and key too.
        """
        data = await self._request(
            "GET",
            f"/wa/app/{self.app_id}/template",
            "listing templates",
            params={"templateStatus": "APPROVED"},
        )
        templates: list[Template] = []
        for item in data.get("templates") or []:
            if item.get("status") != "APPROVED":
                continue
            kind = str(item.get("templateType") or "")
            body = str(item.get("data") or "")
            templates.append(
                Template(
                    id=str(item["id"]),
                    name=str(item.get("elementName") or ""),
                    language=str(item.get("languageCode") or ""),
                    category=str(item.get("category") or "").upper(),
                    body=body,
                    variables=template_variables(None, body, ()),
                    # A media header needs its file passed on every send.
                    unsupported_reason=None
                    if kind == "TEXT"
                    else f"{kind.lower()} templates on Gupshup can't be sent from a batch yet",
                )
            )
        return templates

    async def send_text(self, *, to: str, text: str) -> str:
        """One session message inside the 24h window. ``to`` is the peer address."""
        data = await self._request(
            "POST",
            "/wa/api/v1/msg",
            "sending a message",
            data={
                "channel": "whatsapp",
                "source": self._number(self.sender_e164),
                "destination": to,
                "src.name": self.app_name,
                "message": json.dumps({"type": "text", "text": text}),
            },
        )
        return str(data["messageId"])

    async def send_template(
        self, *, to_e164: str, template: Template, values: dict[str, str], status_callback: str
    ) -> str:
        """Send an approved template. Parameters are positional: `{{1}}`, `{{2}}`…"""
        del status_callback
        params = [values[name] for name in sorted(template.variables, key=int)]
        data = await self._request(
            "POST",
            "/wa/api/v1/template/msg",
            "sending a template",
            data={
                "channel": "whatsapp",
                "source": self._number(self.sender_e164),
                "destination": self._number(to_e164),
                "src.name": self.app_name,
                "template": json.dumps({"id": template.id, "params": params}),
            },
        )
        return str(data["messageId"])

    # ── webhook subscriptions ──────────────────────────────────────────────
    # The same list as the app's Webhooks tab in the Gupshup console.
    # https://partner-docs.gupshup.io/reference/setsubscription-api-v3

    async def subscriptions(self) -> list[dict[str, Any]]:
        data = await self._request(
            "GET", f"/wa/app/{self.app_id}/subscription", "listing the app's webhooks"
        )
        return list(data.get("subscriptions") or [])

    async def subscribe(self, *, tag: str, url: str) -> str:
        """Deliver messages and receipts to ``url`` in the v2 format. Returns its id.

        Gupshup POSTs to the URL before accepting it, and refuses one that does
        not answer 2xx.
        """
        data = await self._request(
            "POST",
            f"/wa/app/{self.app_id}/subscription",
            "registering the webhook",
            data={
                "modes": _SUBSCRIPTION_MODES,
                "tag": tag,
                "url": url,
                "version": "2",
                "showOnUI": "true",
            },
        )
        return str(data["subscription"]["id"])

    async def unsubscribe(self, subscription_id: str) -> None:
        await self._request(
            "DELETE",
            f"/wa/app/{self.app_id}/subscription/{subscription_id}",
            "removing the webhook",
        )

    def parse_webhook(self, payload: dict[str, Any]) -> WebhookEvent | None:
        """Parse one v2 callback. The caller has already checked the URL token."""
        if payload.get("app") != self.app_name:
            raise HTTPException(status_code=403, detail="webhook is for a different Gupshup app")
        event_type = payload.get("type")
        inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}

        if event_type == "message-event":
            status = _EVENT_STATUS.get(str(inner.get("type") or ""))
            if status is None:
                return None
            # `gsId` is Gupshup's id — what the send returned — whenever the event
            # carries one (sent, delivered, read, async failed); otherwise `id` is.
            message_id = str(inner.get("gsId") or inner.get("id") or "")
            if not message_id:
                return None
            detail = inner.get("payload") if isinstance(inner.get("payload"), dict) else {}
            code = detail.get("code")
            return StatusEvent(
                message_id=message_id,
                status=status,
                error_code=str(code) if code is not None else None,
                error=str(detail.get("reason")) if detail.get("reason") else None,
            )

        if event_type != "message":
            # `user-event` (sandbox-start, opted-in…) and anything newer.
            return None
        message_id = str(inner.get("id") or "")
        if not message_id:
            raise HTTPException(status_code=400, detail="Gupshup message has no id")
        sender = inner.get("sender") if isinstance(inner.get("sender"), dict) else {}
        phone = str(sender.get("phone") or inner.get("source") or "").strip().removeprefix("+")
        content = inner.get("payload") if isinstance(inner.get("payload"), dict) else {}
        text = str(content.get("text") or "").strip()
        if inner.get("type") != "text" or not text or not phone:
            return InboundUnsupported(
                message_id=message_id, kind=str(inner.get("type") or "unknown"), raw=payload
            )
        return InboundText(
            message_id=message_id,
            peer=f"+{phone}",
            peer_address=phone,
            text=text,
            profile_name=str(sender.get("name") or "").strip() or None,
            raw=payload,
        )
