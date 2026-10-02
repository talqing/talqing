"""What every WhatsApp BSP client parses into and raises.

Each BSP module turns its own wire format into these shapes; everything after
parsing — conversations, turns, status bookkeeping, batches — is BSP-neutral.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

# WhatsApp's text body limit, for a session message and a template variable alike.
WHATSAPP_MESSAGE_LIMIT = 4096

# Every BSP call's timeout. A batch send must finish and settle inside the job
# executor's `SETTLE_GRACE_SECONDS` (15), so this stays well under it.
BSP_TIMEOUT_SECONDS = 10

# A template placeholder: `{{1}}` on both BSPs, or `{{first_name}}` on Twilio.
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")

# Where a message is on its way to the recipient. `queued` is accepted by the
# BSP; the rest are Meta's own receipts.
MessageStatus = Literal["queued", "sent", "delivered", "read", "undelivered", "failed"]

# How a failed send is handled:
# - `row`: this recipient cannot be messaged (not on WhatsApp, opted out…).
# - `held_back`: Meta withheld a marketing template under the recipient's
#   per-person limit; the same message may go through days later.
# - `rate_limited`: try again shortly; costs no attempt.
# - `transient`: the BSP or the network hiccuped; retry with backoff.
# - `account`: the credential or the business account is broken; stop sending.
# - `template`: the template is gone or paused; stop sending.
# - `wallet`: the BSP wallet is empty; pause until the user tops it up.
FailureKind = Literal[
    "row", "held_back", "rate_limited", "transient", "account", "template", "wallet"
]


class BspError(RuntimeError):
    """A BSP call that failed, classified for whoever retries it."""

    def __init__(
        self,
        message: str,
        *,
        kind: FailureKind,
        code: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.code = code
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True, slots=True)
class TemplateHeader:
    type: Literal["text", "image", "video", "document"]
    text: str | None = None  # type == "text"
    url: str | None = None  # the media, otherwise


@dataclass(frozen=True, slots=True)
class TemplateButton:
    type: Literal["url", "phone_number", "quick_reply", "copy_code", "voice_call"]
    text: str
    url: str | None = None
    phone_number: str | None = None
    code: str | None = None


@dataclass(frozen=True, slots=True)
class Template:
    """One approved template, the whole message, as a batch snapshots it."""

    id: str
    name: str
    language: str
    category: str
    body: str
    header: TemplateHeader | None = None
    footer: str | None = None
    buttons: tuple[TemplateButton, ...] = ()
    # Every placeholder in order of first appearance: header, body, buttons. The
    # list a batch's variable map must cover, and what a send passes.
    variables: tuple[str, ...] = ()
    # Why a batch cannot send it (a carousel, a catalog…), or ``None`` when it can.
    unsupported_reason: str | None = None


@dataclass(frozen=True, slots=True)
class InboundText:
    """A customer's text message to the sender."""

    message_id: str
    # E.164 when the BSP gives a phone number; otherwise a WhatsApp username id.
    peer: str
    # What a reply is addressed to, in the BSP's own format.
    peer_address: str
    text: str
    profile_name: str | None
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class InboundUnsupported:
    """A customer message Talqing does not handle yet (image, voice note…)."""

    message_id: str
    kind: str
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StatusEvent:
    """A delivery receipt for a message we sent, keyed by the id the send returned."""

    message_id: str
    status: MessageStatus
    error_code: str | None
    error: str | None


WebhookEvent = InboundText | InboundUnsupported | StatusEvent


def template_variables(
    header: TemplateHeader | None, body: str, buttons: tuple[TemplateButton, ...]
) -> tuple[str, ...]:
    """Placeholder names across the whole message, in order of first appearance."""
    parts = [header.text, header.url] if header else []
    parts.append(body)
    for button in buttons:
        parts += [button.url, button.code]
    seen: list[str] = []
    for part in parts:
        for name in PLACEHOLDER_RE.findall(part or ""):
            if name not in seen:
                seen.append(name)
    return tuple(seen)


def render_template(text: str, values: dict[str, str]) -> str:
    """``text`` with every placeholder replaced; a missing or blank value stays visible."""
    return PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1)) or m.group(0), text)


def variable_problem(value: str | None) -> str | None:
    """Why a cell cannot be sent as a template variable, or ``None``.

    Meta refuses an empty variable, a newline or tab in one, and more than four
    consecutive spaces (Twilio 63013), and it refuses the whole message.
    """
    if value is None or not value.strip():
        return "is empty"
    if "\n" in value or "\r" in value or "\t" in value:
        return "contains a line break or tab"
    if "     " in value:
        return "contains more than four spaces in a row"
    if len(value) > WHATSAPP_MESSAGE_LIMIT:
        return "is too long"
    return None
