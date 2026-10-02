"""Telephony provider adapter protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from services.telephony.models import TelephonyAccount, TelephonySetupStep


class ProviderError(Exception):
    """Raised for actionable provider/API failures (surfaced as HTTP 400).

    ``status_code`` is the provider's HTTP status when the failure came from an
    API call, so callers can branch on documented statuses instead of matching
    on message text.
    """

    def __init__(self, message: str, *, status_code: int | None = None):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class RemoteNumberInfo:
    e164: str
    provider_number_id: str | None = None
    label: str | None = None


@dataclass(frozen=True, slots=True)
class OutboundInlineConfig:
    hostname: str
    auth_username: str
    auth_password: str
    transport: str = "tcp"  # tcp | tls | udp


class TelephonyProviderAdapter(Protocol):
    provider: str

    async def validate_credentials(
        self, account: TelephonyAccount, secrets: dict[str, str]
    ) -> None:
        """Raise ProviderError if credentials are invalid."""
        ...

    async def list_remote_numbers(
        self, account: TelephonyAccount, secrets: dict[str, str]
    ) -> list[RemoteNumberInfo]: ...

    async def ensure_account_sip(
        self,
        account: TelephonyAccount,
        secrets: dict[str, str],
        *,
        platform_sip_uri: str,
    ) -> tuple[dict[str, Any], str | None]:
        """Create/update shared provider trunks.

        Returns ``(provider_state, generated_sip_password)``. The password is
        non-None only when a new outbound digest was just created (one-shot;
        providers never return it on GET). Caller must store it as a secret.
        """
        ...

    async def bind_number(
        self,
        account: TelephonyAccount,
        secrets: dict[str, str],
        *,
        e164: str,
        number_provider_state: dict[str, Any],
    ) -> dict[str, Any]:
        """Map DID to provider inbound path. Returns number.provider_state."""
        ...

    async def unbind_number(
        self,
        account: TelephonyAccount,
        secrets: dict[str, str],
        *,
        e164: str,
        number_provider_state: dict[str, Any],
    ) -> dict[str, Any]:
        """Undo ``bind_number`` at the carrier. Returns number.provider_state.

        Called when a number is disabled, ahead of the LiveKit teardown, so the
        carrier stops delivering the DID rather than delivering it to a trunk
        that no longer exists. Must be idempotent: a mapping that is already
        gone counts as success.

        Providers with no documented way to unmap a number implement this as a
        no-op, so a clean return is not proof the DID was released.
        """
        ...

    async def teardown_account(self, account: TelephonyAccount, secrets: dict[str, str]) -> None:
        """Delete the carrier-side objects we created for this account.

        Called on account deletion. Only objects whose ids we stored in
        ``provider_state`` are touched — never anything the tenant made
        themselves. Deleting an object that is already gone is not an error,
        so this is safe to retry.

        The tenant's DIDs are left pointing at the deleted inbound trunk.
        Neither provider documents a way to unassign a number, and a DID whose
        trunk no longer exists fails at the carrier rather than being delivered
        to a host that can no longer serve it — which is the honest outcome.
        """
        ...

    def setup_steps(self, account: TelephonyAccount) -> list[TelephonySetupStep]:
        """What still stands between this account and working inbound calls.

        Pure and synchronous — read from ``account.provider_state``, never from
        the carrier's API, because this is rendered on every account list.
        Carriers we can drive entirely over their API return no steps at all.
        """
        ...

    def outbound_sip_configured(self, provider_state: dict[str, Any]) -> bool:
        """True when ``ensure_account_sip`` has set up this carrier's outbound identity.

        The keys that answer that differ per carrier, so each adapter reads its
        own — the same ones ``outbound_inline_config`` reads. Unlike that
        method this never raises: it is a predicate the caller uses to decide
        whether the outbound password secret still has to resolve.
        """
        ...

    def outbound_inline_config(
        self,
        account: TelephonyAccount,
        secrets: dict[str, str],
        *,
        from_e164: str,
    ) -> OutboundInlineConfig: ...

    def refer_uri(self, account: TelephonyAccount, *, e164: str, direction: str) -> str:
        """The ``Refer-To`` URI this carrier requires to transfer a live call.

        Carriers do not agree on what they accept here. Plivo blocks ``tel:``
        and any host that is not the account's own ``.zt.plivo.com`` trunk;
        Twilio accepts both forms. Both are given the same ``sip:`` shape so
        there is one code path, and a missing ``provider_state`` key raises
        rather than falling back — the fallback would fail at REFER time, on a
        live call, with the caller listening.

        ``direction`` is ``inbound`` or ``outbound``, the direction of the call
        being transferred. Carriers that keep a separate trunk per direction
        (Plivo, Vobiz) name the matching one; carriers with a single trunk
        (Twilio) ignore it. Whether the host has to match the trunk the call
        arrived on is not documented by anyone — this is the shape under test.

        Adapters whose spec says ``supports_refer=False`` raise: reaching this
        is a routing bug, not a carrier limitation to work around.
        """
        ...


def get_adapter(provider: str) -> TelephonyProviderAdapter:
    from .exotel import ExotelAdapter
    from .plivo import PlivoAdapter
    from .twilio import TwilioAdapter
    from .vobiz import VobizAdapter

    if provider == "plivo":
        return PlivoAdapter()
    if provider == "exotel":
        return ExotelAdapter()
    if provider == "vobiz":
        return VobizAdapter()
    if provider == "twilio":
        return TwilioAdapter()
    raise ProviderError(f"unknown telephony provider: {provider}")
