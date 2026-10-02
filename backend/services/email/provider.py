"""The email provider seam: one message, one receipt, four failure classes.

Resend is the only implementation today and its terms forbid the very use case
this feature exists for (see ``services.email.batch.service`` and the
``create_email_batch`` docstring), so hard-wiring it would make the second
provider a rewrite of the batch rather than a new file. The seam is here from
the first line of code for the same reason telephony has one.

**Talqing never asks a model to send an email.** Resend's MCP server exists and
agents may use it; the batch does not. Sending happens after the human review
gate, from our backend, over the provider's REST API, with an idempotency key, a
pace and a retry policy — none of which an LLM tool call can be made to have.
"""

from __future__ import annotations

from dataclasses import dataclass
from email.utils import formataddr
from typing import Literal, Protocol
from uuid import UUID

# Whose problem a failed send is, and therefore what happens next.
#
# - ``rate_limited``  NOBODY's problem, and it is its own class for exactly that
#                  reason. The row goes back to `queued` at the provider's own
#                  `retry-after`, spends no attempt and does not move the
#                  breaker. A provider's rate limit is usually shared across a
#                  whole account — Resend's 10 rps covers every key, the
#                  tenant's own password-reset mail included — so charging a
#                  reviewed draft three strikes for somebody else's traffic put
#                  it in `send_failed`, which nothing but a paid redraft
#                  recovers.
# - ``transient``  something went wrong and might not next time: a 5xx, a
#                  timeout, an accepted send with no id. The row goes back to
#                  `queued` after `send_retry_after_minutes`, spends an attempt,
#                  and moves the breaker.
# - ``row``        this row is permanently unsendable; the handler settles it
#                  `send_failed` and CONTINUES with the next one. The breaker is
#                  neither moved nor reset — one bad address is a fact about that
#                  row's data.
# - ``account``    every remaining row will fail identically — a revoked key, an
#                  unverified sending domain, a contact quota. The run stops now
#                  rather than after ten, because a rolling counter is the right
#                  shape for probabilistic failure and the wrong shape for
#                  deterministic failure.
SendFailureClass = Literal["rate_limited", "transient", "row", "account"]

BodyFormat = Literal["text", "html"]


@dataclass(frozen=True, slots=True)
class EmailMessage:
    """One email, fully resolved. Nothing here is looked up again downstream."""

    to: str
    subject: str
    body: str
    body_format: BodyFormat
    from_email: str
    from_name: str | None = None
    reply_to: str | None = None

    @property
    def from_header(self) -> str:
        """``Name <address>`` when there is a name, the bare address otherwise.

        **Quoted per RFC 5322**, and that is not decoration: `from_name` is free
        text a tenant types, and a comma in it — `Acme, Inc.` — turns one address
        into an address LIST, which Resend rejects one row at a time. Interpolating
        it by hand is what this replaced.

        `formataddr` from the standard library does the quoting and the escaping,
        so there is no hand-rolled parser here to be wrong about a display name
        with a quote in it.
        """
        return formataddr((self.from_name, self.from_email)) if self.from_name else self.from_email

    @property
    def headers(self) -> dict[str, str]:
        """Extra headers every send carries.

        **`List-Unsubscribe`, RFC 2369, and one URL per pair of angle brackets.**
        The subject is a `mailto:` query parameter — `?subject=`, never a comma,
        which would make it a second URL. The address is the reply-to when there
        is one, because that is where this tenant's replies already go.

        Deliberately NOT one-click (`List-Unsubscribe-Post` plus an HTTPS
        endpoint): one-click needs somewhere to record the opt-out, and there is
        no suppression store yet. Shipping the header without one would be a
        button that silently does nothing, which is worse than a `mailto:` that
        lands in a real inbox. It arrives with bounce ingestion.
        """
        address = self.reply_to or self.from_email
        return {"List-Unsubscribe": f"<mailto:{address}?subject=unsubscribe>"}


@dataclass(frozen=True, slots=True)
class SendReceipt:
    """What the provider gave back. ``id`` is stored on the row, and is what a
    person looks the email up by in the provider's own dashboard."""

    id: str


class EmailProviderError(RuntimeError):
    """A provider refused or could not be reached.

    ``retry_after_seconds`` is carried here rather than left to the classifier
    because it is the provider's own answer to "when should you come back", and
    honouring it is the whole of what stops a burst becoming a retry storm —
    this feature has no lock and no shared rate limiter, so a 429 IS the
    backpressure mechanism.
    """

    def __init__(self, message: str, *, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.retry_after_seconds = retry_after_seconds


class EmailProvider(Protocol):
    """What a batch needs from an email provider, and nothing more."""

    provider: str

    async def send(
        self, *, credential: str, message: EmailMessage, idempotency_key: str
    ) -> SendReceipt:
        """Send exactly one email and return the provider's id for it.

        ``idempotency_key`` is the recipient row's own id, and honouring it is
        what closes the one crash window this design cannot close itself: we
        called the provider, it accepted, we died before writing the id. A
        re-send under the same key must return the original response without
        sending a second email.

        One email per request, deliberately. A batch endpoint's idempotency key
        is per *request* rather than per email, and the composition of a chunk
        depends on which rows happened to be claimable — so a retried chunk is a
        different chunk and the key stops meaning anything.

        Raises ``EmailProviderError`` for anything the provider said, and lets
        transport errors through untouched; ``classify_error`` reads both, and
        ``retry_after_seconds`` on the error is what a `rate_limited` row waits.
        """
        ...

    async def verified_senders(self, *, credential: str) -> list[str]:
        """The domains this account may send from, right now.

        Checked live when a batch is created, and again whenever a send
        OVERRIDES `from_email` — because "that domain is not verified" is a
        sentence an operator can act on and fifty `send_failed` rows twenty
        minutes later is not. A send that inherits the batch's sender is not
        re-checked: the account-class failure on its first email stops it and
        hands every other row back as a draft.
        """
        ...

    def classify_error(self, exc: Exception) -> SendFailureClass:
        """Whose problem this is. Lives on the provider because the next one's
        status codes will not be this one's."""
        ...


def idempotency_key(recipient_id: UUID) -> str:
    """The key one row's send carries, forever.

    The row's own id, because it is the one identifier that is stable across
    every retry of every job that could ever touch this row — a per-attempt or
    per-job key would make the retry a second real email.
    """
    return str(recipient_id)
