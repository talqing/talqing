"""Resend, behind the `EmailProvider` seam.

Docs:
  send      https://resend.com/docs/api-reference/emails/send-email
  domains   https://resend.com/docs/api-reference/domains/list-domains
  limits    https://resend.com/docs/api-reference/rate-limit
  errors    https://resend.com/docs/api-reference/errors

Two facts about this account that the batch is built around:

- **10 requests/second per team**, across every API key. A send's
  `send_gap_seconds` has a floor of 1 s and sending is serial, so one send can
  never reach it; several concurrent sends can, and a `429` is exactly what the
  transient class is for.
- **`Idempotency-Key` is retained for 24 hours**, max 256 characters. That
  window is what closes the crash-after-send hole. A send pass is bounded, so
  the key only ever has to cover ONE row's send rather than a whole run — which
  is what lets a run pace itself over days.

Verified against the live API on 2026-08-23: an error body is
``{"statusCode": 400, "message": "API key is invalid", "name": "validation_error"}``
and `GET /domains` returns ``{"object", "has_more", "data": [{"id", "name",
"status", "created_at", "region", "capabilities": {...}, ...}]}``.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..provider import EmailMessage, EmailProviderError, SendFailureClass, SendReceipt

RESEND_API_BASE = "https://api.resend.com"
# Generous next to a send that normally answers in well under a second, and
# short enough that a hung provider does not hold a job open for minutes. The
# send job's own pacing is a different number and lives on the batch.
_HTTP_TIMEOUT_SECONDS = 20.0

# Codes that describe THE ACCOUNT rather than this email: every remaining row
# fails identically, so the batch stops on the first one.
_ACCOUNT_ERROR_CODES = frozenset(
    {
        "missing_api_key",
        "restricted_api_key",
        "invalid_api_key",
        "suspended_api_key",
        "invalid_permission",
        "daily_quota_exceeded",
        "monthly_quota_exceeded",
    }
)


class ResendApiError(EmailProviderError):
    """Resend answered, and said no."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        code: str,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message, retry_after_seconds=retry_after_seconds)
        self.status_code = status_code
        self.code = code


def _api_error(response: httpx.Response) -> ResendApiError:
    """Read Resend's error body, which is `{statusCode, message, name}`."""
    code = ""
    message = ""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        code = str(body.get("name") or "").strip()
        message = str(body.get("message") or "").strip()
    if not message:
        message = response.text[:300] or f"HTTP {response.status_code}"
    retry_after = response.headers.get("retry-after") or response.headers.get("ratelimit-reset")
    try:
        retry_after_seconds = max(1, int(float(retry_after))) if retry_after else None
    except ValueError:
        # Resend documents seconds, but an HTTP-date is legal on `Retry-After`
        # and the job's own 1/2/4/8-minute backoff already covers us.
        retry_after_seconds = None
    return ResendApiError(
        message,
        status_code=response.status_code,
        code=code,
        retry_after_seconds=retry_after_seconds,
    )


class ResendEmailProvider:
    """The `EmailProvider` implementation. Stateless — one per process is fine."""

    provider = "resend"

    async def send(
        self, *, credential: str, message: EmailMessage, idempotency_key: str
    ) -> SendReceipt:
        payload: dict[str, Any] = {
            "from": message.from_header,
            "to": [message.to],
            "subject": message.subject,
            ("html" if message.body_format == "html" else "text"): message.body,
            # `POST /emails` takes arbitrary custom headers, which is the only
            # route to `List-Unsubscribe`: Resend's own unsubscribe machinery is
            # Broadcasts / Audiences / Contacts and never touches the
            # transactional send path a batch uses.
            "headers": message.headers,
        }
        if message.reply_to:
            payload["reply_to"] = message.reply_to
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{RESEND_API_BASE}/emails",
                json=payload,
                headers={
                    "Authorization": f"Bearer {credential}",
                    "Idempotency-Key": idempotency_key,
                },
            )
        if response.status_code >= 400:
            raise _api_error(response)
        body = response.json()
        message_id = str(body.get("id") or "").strip() if isinstance(body, dict) else ""
        if not message_id:
            # Nothing to record and nothing to reconcile against. Transient by
            # classification, so the job retries under the same key and Resend
            # replays whatever it decided the first time.
            raise ResendApiError(
                "Resend accepted the email but returned no id",
                status_code=response.status_code,
                code="missing_id",
            )
        return SendReceipt(id=message_id)

    async def verified_senders(self, *, credential: str) -> list[str]:
        """Every domain that may send on this account right now.

        Only `status == "verified"` with sending enabled counts. A domain that is
        merely *added* cannot send, and offering it in a picker would turn a
        clear 400 at the gate into fifty failures twenty minutes later.
        """
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.get(
                f"{RESEND_API_BASE}/domains",
                headers={"Authorization": f"Bearer {credential}"},
            )
        if response.status_code >= 400:
            raise _api_error(response)
        body = response.json()
        domains = body.get("data") if isinstance(body, dict) else None
        if not isinstance(domains, list):
            raise ResendApiError(
                "Resend returned an unexpected domain list",
                status_code=response.status_code,
                code="unexpected_body",
            )
        out: list[str] = []
        for entry in domains:
            if not isinstance(entry, dict):
                continue
            capabilities = entry.get("capabilities")
            sending = (
                str(capabilities.get("sending") or "") if isinstance(capabilities, dict) else ""
            )
            # `capabilities` is recent; an account that predates it reports
            # nothing there, and `status == verified` is the older answer to the
            # same question. Treat an absent capability block as "not disabled".
            if str(entry.get("status") or "") != "verified" or sending == "disabled":
                continue
            name = str(entry.get("name") or "").strip().lower()
            if name:
                out.append(name)
        return sorted(set(out))

    def classify_error(self, exc: Exception) -> SendFailureClass:
        """Whose problem this is.

        Anything unrecognised is `transient`: a send that might have landed must
        not be written off as permanently failed, because the retry is free —
        the idempotency key makes a re-send that already succeeded a no-op.
        """
        if isinstance(exc, ResendApiError):
            if exc.code in _ACCOUNT_ERROR_CODES:
                return "account"
            if exc.status_code == 429:
                # Its own class, not `transient`: Resend's 10 rps is shared
                # across every key on the team, so a 429 usually says nothing
                # about this row or this send.
                return "rate_limited"
            if exc.status_code in (401, 403):
                # A 403 with no recognised code is still about the credential or
                # the account's standing, never about this one recipient.
                return "account"
            if exc.status_code in (400, 422):
                # Resend answers an unverified sending domain with a 400
                # `validation_error` naming the domain — an account-level fact
                # wearing a per-row status code, and the one case where reading
                # the message is the only way to tell them apart.
                lowered = exc.message.lower()
                if "domain is not verified" in lowered or "verify a domain" in lowered:
                    return "account"
                if "api key is invalid" in lowered:
                    return "account"
                return "row"
            if exc.status_code >= 500:
                return "transient"
            return "transient"
        if isinstance(exc, httpx.HTTPError):
            return "transient"
        return "transient"


resend_provider = ResendEmailProvider()
