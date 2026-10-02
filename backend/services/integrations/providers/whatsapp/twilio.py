"""Twilio's WhatsApp API: senders, templates, sends and signed webhooks.

Auth is HTTP basic ``(account_sid, auth_token)``. The auth token rather than an
API key, because Twilio signs every webhook with the account's auth token and
nothing else can verify one.

- Senders: https://www.twilio.com/docs/whatsapp/api/senders
- Templates: https://www.twilio.com/docs/content/content-api-resources
- Template types: https://www.twilio.com/docs/content/content-types-overview
- Sending: https://www.twilio.com/docs/content/send-templates-created-with-the-content-template-builder
- Webhooks: https://www.twilio.com/docs/messaging/guides/webhook-request
- Signatures: https://www.twilio.com/docs/usage/security#validating-requests
- Calls: https://www.twilio.com/docs/voice/whatsapp-business-calling
- TwiML Apps: https://www.twilio.com/docs/usage/api/applications
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qsl, urlparse

import httpx
from fastapi import HTTPException

from .bsp import (
    BSP_TIMEOUT_SECONDS,
    BspError,
    InboundText,
    InboundUnsupported,
    MessageStatus,
    StatusEvent,
    Template,
    TemplateButton,
    TemplateHeader,
    WebhookEvent,
    template_variables,
)

_API = "https://api.twilio.com/2010-04-01"
_SENDERS = "https://messaging.twilio.com/v2/Channels/Senders"
_CONTENT = "https://content.twilio.com/v2/ContentAndApprovals"
_TYPING = "https://messaging.twilio.com/v3/Indicators/Typing.json"

# https://www.twilio.com/docs/api/errors — the codes that decide an outcome.
_ROW_CODES = {
    "21211",  # invalid To
    "21614",  # To is not a valid mobile number
    "63003",  # invalid To / channel address
    "63013",  # policy: empty variable, newline, >4 spaces
    "63016",  # outside the 24h window
    "63024",  # recipient not on WhatsApp
    "63033",  # recipient opted out of the business's messages
    "63050",  # recipient opted out of marketing
    "63058",  # the business is restricted from messaging this country
}
# Meta did not deliver a marketing template: the recipient's per-person limit, or
# a US number, where marketing templates are never delivered.
_HELD_BACK_CODES = {"63049"}
# Refusals any cold list produces in bulk — not on WhatsApp, held back by Meta —
# which say nothing about the batch, so the delivery breaker does not count them.
EXPECTED_REFUSAL_CODES = frozenset({"63024", *_HELD_BACK_CODES})
_TEMPLATE_CODES = {
    "63027",  # template missing
    "21656",  # bad ContentVariables
    "63028",  # variable count mismatch
    "63040",  # template rejected
    "63041",  # template paused
    "63042",  # template disabled
}
_ACCOUNT_CODES = {
    "20003",
    "20404",
    "63112",
    "63051",
    "63007",
    "63020",  # Meta Business Manager has not accepted Twilio
}
_RATE_LIMIT_CODES = {
    "63018",  # rate limit, or the business portfolio's messaging limit
    "63038",  # the account's daily message limit
    "20429",
}

# Content type -> the fields holding its body and its footer. `media[]`,
# `header_text` and `actions[]` are named the same wherever a type has them.
_TEXT_FIELDS: dict[str, tuple[str, str | None]] = {
    "twilio/text": ("body", None),
    "twilio/media": ("body", None),
    "twilio/quick-reply": ("body", None),
    "twilio/call-to-action": ("body", None),
    "twilio/card": ("title", "subtitle"),
    "whatsapp/card": ("body", "footer"),
}
_BUTTON_TYPES: dict[str, str] = {
    "URL": "url",
    "PHONE_NUMBER": "phone_number",
    "PHONE": "phone_number",
    "QUICK_REPLY": "quick_reply",
    "COPY_CODE": "copy_code",
    "VOICE_CALL": "voice_call",
}
_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
_VIDEO_EXTENSIONS = (".mp4", ".3gp")

_TWILIO_STATUS: dict[str, MessageStatus] = {
    "accepted": "queued",
    "scheduled": "queued",
    "queued": "queued",
    "sending": "queued",
    "sent": "sent",
    "delivered": "delivered",
    "read": "read",
    "undelivered": "undelivered",
    "failed": "failed",
}


def _address(e164: str) -> str:
    return f"whatsapp:{e164}"


def webhook_peer(form: Mapping[str, str]) -> str:
    """Who a message or a call is from, as the conversation key names them.

    One derivation for both webhooks, because a call and a chat from the same
    person must land on the same thread. A message carries `WaId`; a call does
    not (measured 2026-09-28), so its `From` is what is left, and for a user
    who shows their number the two give the same `+E164`. `From` is
    `whatsapp:CC.<id>` for a user who hides it.
    """
    wa_id = (form.get("WaId") or "").strip()
    if wa_id:
        return f"+{wa_id}"
    external = (form.get("ExternalUserId") or "").strip()
    return external or (form.get("From") or "").strip().removeprefix("whatsapp:")


def classify(code: str | None, http_status: int | None) -> str:
    if code in _RATE_LIMIT_CODES or http_status == 429:
        return "rate_limited"
    if code in _ACCOUNT_CODES or http_status in (401, 403):
        return "account"
    if code in _TEMPLATE_CODES:
        return "template"
    if code in _ROW_CODES:
        return "row"
    if code in _HELD_BACK_CODES:
        return "held_back"
    if http_status is None or http_status >= 500:
        return "transient"
    return "row"


def _template(content: dict[str, Any]) -> Template:
    """One approved content as the whole WhatsApp message it sends."""
    approval = content.get("approval_requests") or {}
    types = content.get("types") or {}
    # One content has one type (measured 2026-10-01); several is not ours to pick from.
    kind, fields = next(iter(types.items())) if len(types) == 1 else ("", {})
    actions = fields.get("actions") or []
    template = Template(
        id=str(content["sid"]),
        name=str(content.get("friendly_name") or approval.get("name") or ""),
        language=str(content.get("language") or ""),
        category=str(approval.get("category") or "").upper(),
        body=str(fields.get("body") or ""),
    )
    if kind not in _TEXT_FIELDS:
        label = kind.split("/")[-1].replace("-", " ") if kind else "multi-type"
        return replace(
            template, unsupported_reason=f"{label} templates can't be sent from a batch yet"
        )
    if unknown := next((a["type"] for a in actions if a["type"] not in _BUTTON_TYPES), None):
        return replace(
            template,
            unsupported_reason=f"templates with a {unknown} button can't be sent from a batch yet",
        )

    body_field, footer_field = _TEXT_FIELDS[kind]
    body = str(fields[body_field])
    header = None
    if fields.get("header_text"):
        header = TemplateHeader(type="text", text=str(fields["header_text"]))
    elif fields.get("media"):
        url = str(fields["media"][0])
        path = urlparse(url).path.lower()
        header = TemplateHeader(
            type="image"
            if path.endswith(_IMAGE_EXTENSIONS)
            else "video"
            if path.endswith(_VIDEO_EXTENSIONS)
            else "document",
            url=url,
        )
    buttons = tuple(
        TemplateButton(
            type=_BUTTON_TYPES[a["type"]],  # type: ignore[arg-type]
            text=str(a["title"]),
            url=a.get("url"),
            phone_number=a.get("phone"),
            code=a.get("code"),
        )
        for a in actions
    )
    return replace(
        template,
        header=header,
        body=body,
        footer=str(fields[footer_field]) if footer_field and fields.get(footer_field) else None,
        buttons=buttons,
        variables=template_variables(header, body, buttons),
    )


def _error(response: httpx.Response, action: str) -> BspError:
    try:
        data = response.json()
    except ValueError:
        data = {}
    code = str(data.get("code")) if isinstance(data, dict) and data.get("code") else None
    message = data.get("message") if isinstance(data, dict) else None
    detail = f"Twilio {action} failed (HTTP {response.status_code}"
    detail += f", error {code})" if code else ")"
    if message:
        detail += f": {message}"
    retry_after = response.headers.get("retry-after")
    return BspError(
        detail,
        kind=classify(code, response.status_code),  # type: ignore[arg-type]
        code=code,
        retry_after_seconds=float(retry_after) if retry_after and retry_after.isdigit() else None,
    )


class TwilioWhatsApp:
    bsp = "twilio"

    def __init__(self, *, account_sid: str, auth_token: str, sender_e164: str) -> None:
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.sender_e164 = sender_e164

    def peer_address(self, e164: str) -> str:
        """How a reply to ``e164`` is addressed."""
        return _address(e164)

    async def _request(
        self,
        method: str,
        url: str,
        action: str,
        *,
        data: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=BSP_TIMEOUT_SECONDS) as client:
                response = await client.request(
                    method,
                    url,
                    auth=(self.account_sid, self.auth_token),
                    data=data,
                    json=json_body,
                    params=params,
                )
        except httpx.RequestError as exc:
            raise BspError(f"Twilio {action} failed: {exc}", kind="transient") from exc
        if not response.is_success:
            raise _error(response, action)
        if response.status_code == 204:
            return {}
        return response.json()

    # ── account ────────────────────────────────────────────────────────────

    async def list_senders(self) -> list[dict[str, str]]:
        """Every WhatsApp sender on the account: ``{e164, sid, status}``."""
        senders: list[dict[str, str]] = []
        url: str | None = _SENDERS
        params: dict[str, str] | None = {"Channel": "whatsapp", "PageSize": "100"}
        while url:
            page = await self._request("GET", url, "listing senders", params=params)
            for sender in page.get("senders") or []:
                sender_id = str(sender.get("sender_id") or "")
                if sender_id.startswith("whatsapp:"):
                    senders.append(
                        {
                            "e164": sender_id.removeprefix("whatsapp:"),
                            "sid": str(sender["sid"]),
                            "status": str(sender.get("status") or ""),
                        }
                    )
            url = (page.get("meta") or {}).get("next_page_url")
            params = None
        return senders

    async def require_online_sender(self) -> dict[str, str]:
        """This integration's sender, refusing one the account cannot send from."""
        senders = await self.list_senders()
        sender = next((s for s in senders if s["e164"] == self.sender_e164), None)
        if sender is None:
            raise BspError(
                f"{self.sender_e164} is not a WhatsApp sender on Twilio account {self.account_sid}",
                kind="account",
            )
        if sender["status"] != "ONLINE":
            raise BspError(
                f"Twilio reports sender {self.sender_e164} as {sender['status']}; "
                "it can send once it is ONLINE",
                kind="account",
            )
        return sender

    async def set_inbound_webhook(self, sender_sid: str, url: str) -> None:
        """Point the sender's inbound messages at ``url``."""
        await self._request(
            "POST",
            f"{_SENDERS}/{sender_sid}",
            "setting the sender's webhook",
            json_body={"webhook": {"callback_url": url, "callback_method": "POST"}},
        )

    async def clear_inbound_webhook(self, sender_sid: str, url: str) -> None:
        """Clear the sender's inbound webhook, but only while it is still ``url``.

        A URL the user pointed somewhere else after we set ours is theirs.
        """
        sender = await self._request("GET", f"{_SENDERS}/{sender_sid}", "reading the sender")
        if (sender.get("webhook") or {}).get("callback_url") != url:
            return
        await self._request(
            "POST",
            f"{_SENDERS}/{sender_sid}",
            "clearing the sender's webhook",
            json_body={"webhook": {"callback_url": ""}},
        )

    # ── calls ──────────────────────────────────────────────────────────────

    async def voice_application_sid(self, sender_sid: str) -> str:
        """The TwiML App the sender's calls go to, or ``""`` when calling is off.

        An unset value reads back as ``""``, not null.
        """
        sender = await self._request("GET", f"{_SENDERS}/{sender_sid}", "reading the sender")
        return str((sender.get("configuration") or {}).get("voice_application_sid") or "")

    async def set_voice_application(self, sender_sid: str, application_sid: str) -> None:
        """Send the sender's calls to ``application_sid``; ``""`` turns calling off.

        Not null, whatever the docs say: Twilio refuses a null with 63100
        ("Update request body is empty"). It does not check the SID exists.
        """
        await self._request(
            "POST",
            f"{_SENDERS}/{sender_sid}",
            "setting the sender's calling",
            json_body={"configuration": {"voice_application_sid": application_sid}},
        )

    async def create_application(self, *, friendly_name: str, voice_url: str) -> str:
        data = await self._request(
            "POST",
            f"{_API}/Accounts/{self.account_sid}/Applications.json",
            "creating the calls app",
            data={"FriendlyName": friendly_name, "VoiceUrl": voice_url, "VoiceMethod": "POST"},
        )
        return str(data["sid"])

    async def application_voice_url(self, application_sid: str) -> str | None:
        """The app's Voice URL, or ``None`` when no such app exists on the account."""
        try:
            data = await self._request(
                "GET",
                f"{_API}/Accounts/{self.account_sid}/Applications/{application_sid}.json",
                "reading the calls app",
            )
        except BspError as exc:
            if exc.code == "20404":
                return None
            raise
        return str(data.get("voice_url") or "")

    async def applications_named(self, friendly_name: str) -> list[dict[str, Any]]:
        data = await self._request(
            "GET",
            f"{_API}/Accounts/{self.account_sid}/Applications.json",
            "listing calls apps",
            params={"FriendlyName": friendly_name, "PageSize": "50"},
        )
        return list(data.get("applications") or [])

    async def delete_application(self, application_sid: str) -> None:
        try:
            await self._request(
                "DELETE",
                f"{_API}/Accounts/{self.account_sid}/Applications/{application_sid}.json",
                "deleting the calls app",
            )
        except BspError as exc:
            if exc.code != "20404":
                raise

    async def list_templates(self) -> list[Template]:
        """Every template WhatsApp approved on this account, sendable from a batch or not."""
        templates: list[Template] = []
        url: str | None = _CONTENT
        params: dict[str, str] | None = {
            "ChannelEligibility": "whatsapp:approved",
            "PageSize": "100",
        }
        while url:
            page = await self._request("GET", url, "listing templates", params=params)
            for content in page.get("contents") or []:
                if (content.get("approval_requests") or {}).get("status") == "approved":
                    templates.append(_template(content))
            url = (page.get("meta") or {}).get("next_page_url")
            params = None
        return templates

    # ── sending ────────────────────────────────────────────────────────────

    async def send_text(self, *, to: str, text: str) -> str:
        """One session message inside the 24h window. ``to`` is the peer address.

        No `StatusCallback`: nothing records receipts for an agent's reply.
        """
        data = await self._request(
            "POST",
            f"{_API}/Accounts/{self.account_sid}/Messages.json",
            "sending a message",
            data={
                "From": _address(self.sender_e164),
                "To": to,
                "Body": text,
            },
        )
        return str(data["sid"])

    async def send_template(
        self, *, to_e164: str, template: Template, values: dict[str, str], status_callback: str
    ) -> str:
        """Send an approved template. Never with a `Body` (Twilio 63030)."""
        data = await self._request(
            "POST",
            f"{_API}/Accounts/{self.account_sid}/Messages.json",
            "sending a template",
            data={
                "From": _address(self.sender_e164),
                "To": _address(to_e164),
                "ContentSid": template.id,
                "ContentVariables": json.dumps({name: values[name] for name in template.variables}),
                "StatusCallback": status_callback,
            },
        )
        return str(data["sid"])

    async def find_sent_template(self, *, to_e164: str, since: datetime) -> str | None:
        """The SID of a message we sent ``to_e164`` since ``since``, if Twilio has one.

        Twilio has no idempotency key, so this is how a send whose outcome a
        crash lost is adopted rather than sent twice. `DateSent>=` filters by day,
        so the exact instant is checked here.
        """
        data = await self._request(
            "GET",
            f"{_API}/Accounts/{self.account_sid}/Messages.json",
            "looking up a message",
            params={
                "To": _address(to_e164),
                "From": _address(self.sender_e164),
                "DateSent>": (since - timedelta(days=1)).date().isoformat(),
                "PageSize": "20",
            },
        )
        for message in data.get("messages") or []:
            created = message.get("date_created")
            # RFC 2822 (`Sat, 27 Sep 2026 11:53:18 +0000`).
            if created and parsedate_to_datetime(created) >= since - timedelta(seconds=5):
                return str(message["sid"])
        return None

    async def keep_typing_once(self, inbound_message_sid: str) -> None:
        """Mark the message read and show "typing…" for up to 25 seconds."""
        await self._request(
            "POST",
            _TYPING,
            "showing typing",
            # Uppercase, as Twilio's own example sends it: `whatsapp` (what its parameter
            # table says) is refused with a 400. Measured 2026-09-27.
            json_body={"channel": "WHATSAPP", "messageId": inbound_message_sid},
        )

    # ── webhooks ───────────────────────────────────────────────────────────

    def verified_form(
        self, *, url: str, headers: Mapping[str, str], raw_body: bytes
    ) -> dict[str, str]:
        """The posted parameters, once ``X-Twilio-Signature`` checks out.

        The signature covers the exact URL Twilio called plus EVERY posted
        parameter, so it is checked over whatever arrived rather than a fixed
        field list — Twilio adds parameters without notice.
        """
        params = parse_qsl(raw_body.decode("utf-8"), keep_blank_values=True)
        payload = "".join(k + v for k, v in sorted(params)).encode("utf-8")
        expected = base64.b64encode(
            hmac.new(
                self.auth_token.encode("utf-8"), url.encode("utf-8") + payload, hashlib.sha1
            ).digest()
        ).decode("ascii")
        provided = headers.get("x-twilio-signature") or ""
        if not hmac.compare_digest(provided, expected):
            raise HTTPException(status_code=403, detail="invalid Twilio signature")
        return dict(params)

    def parse_webhook(
        self, *, url: str, headers: Mapping[str, str], raw_body: bytes
    ) -> WebhookEvent | None:
        """Verify and parse one inbound message or receipt."""
        form = self.verified_form(url=url, headers=headers, raw_body=raw_body)
        message_sid = form.get("MessageSid") or form.get("SmsSid") or ""
        if not message_sid:
            raise HTTPException(status_code=400, detail="Twilio webhook has no MessageSid")

        if "MessageStatus" in form:
            status = _TWILIO_STATUS.get(form["MessageStatus"])
            if status is None:
                return None
            code = form.get("ErrorCode") or None
            return StatusEvent(
                message_id=message_sid,
                status=status,
                error_code=code,
                error=form.get("ErrorMessage") or None,
            )

        # Only messages TO this sender are ours to answer.
        if form.get("To") != _address(self.sender_e164):
            return None
        body = (form.get("Body") or "").strip()
        if not body:
            kind = "media" if int(form.get("NumMedia") or 0) else "empty"
            return InboundUnsupported(message_id=message_sid, kind=kind, raw=form)
        return InboundText(
            message_id=message_sid,
            peer=webhook_peer(form),
            peer_address=form.get("From") or "",
            text=body,
            profile_name=(form.get("ProfileName") or "").strip() or None,
            raw=form,
        )
