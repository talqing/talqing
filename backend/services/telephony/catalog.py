"""Telephony provider catalog — product metadata (not integrations catalog).

Public API shapes live in ``models`` (``TelephonyProviderSpec``, …).
This module owns the in-process provider registry and lookup helpers.
"""

from __future__ import annotations

from .models import TelephonyProviderSpec, TelephonySetupField

# Carrier marks, served from each carrier's own site the way the integrations
# catalog does. Plivo is the odd one out: it has no /favicon.ico, only the
# sized PNGs its <link rel="icon"> tags point at.
_LOGOS = {
    "plivo": "https://www.plivo.com/favicon/favicon-32x32.png",
    "exotel": "https://exotel.com/favicon.ico",
    "vobiz": "https://vobiz.ai/favicon.ico",
    "twilio": "https://www.twilio.com/favicon.ico",
}

# ── why `supports_refer` is False everywhere (temporary) ─────────────────────
#
# SIP REFER works in exactly one direction per carrier, and the directions are
# opposite. Measured against live trunks:
#
#     carrier   inbound                     outbound
#     Plivo     405 Method Not Allowed      works
#     Vobiz     works                       202 then no leg is ever placed
#
# It is not the `Refer-To` host: pointing it at the trunk the call arrived on
# instead of the outbound trunk changed neither result. LiveKit addresses an
# in-dialog REFER at the `Contact` from the carrier's own INVITE
# (`sip/pkg/sip/inbound.go::swapSrcDst`), so on the failing directions the
# request reaches a node that refuses the method before it ever reads our
# header — nothing on our side selects that node.
#
# So every carrier is on the blind bridge until that is settled with the
# carriers themselves. The bridge reaches a human in all four cases; what it
# costs is the caller ID the human sees (the tenant's DID rather than the
# caller's number) and a worker job slot
# held for the length of the human-to-human call.
TELEPHONY_PROVIDERS: dict[str, TelephonyProviderSpec] = {
    "plivo": TelephonyProviderSpec(
        provider="plivo",
        label="Plivo",
        description="BYO Plivo Zentrunk SIP for inbound and outbound PSTN calls.",
        logo_url=_LOGOS["plivo"],
        account_info_keys=["auth_id"],
        credential_keys=["auth_token"],
        implemented=True,
        # Plivo documents REFER on both directions with no enablement step
        # (https://www.plivo.com/docs/sip-trunking/concepts/sip-refer), and
        # outbound does work — but an inbound transfer is refused with
        # `405 Method Not Allowed`, a status their own REFER page does not even
        # list. See the note on `supports_refer` below for why all four carriers
        # are on the bridge for now.
        supports_refer=False,
        setup_fields=[
            TelephonySetupField(
                key="auth_id",
                label="Auth ID",
                type="text",
                target="account_info",
                placeholder="MAxxxxxxxx",
                hint="From the Plivo console home page.",
            ),
            TelephonySetupField(
                key="auth_token",
                label="Auth Token",
                type="secret_ref",
                target="credentials",
                placeholder="Auth token from Plivo console",
                hint="Paste the auth token. Talqing stores it as an encrypted secret.",
            ),
        ],
    ),
    "exotel": TelephonyProviderSpec(
        provider="exotel",
        label="Exotel",
        description=(
            "BYO Exotel Dynamic SIP Trunking. Trunks are automated; inbound "
            "needs a one-off App Bazaar Flow set up in the Exotel console."
        ),
        logo_url=_LOGOS["exotel"],
        account_info_keys=["account_sid", "region", "subdomain"],
        credential_keys=["api_key", "api_token"],
        implemented=True,
        # REFER appears in Exotel's `Allow` header, but their trunking product is
        # still Alpha and documents no transfer flow at all. Transfers ship on the
        # bridge here until someone tests a real trunk.
        supports_refer=False,
        # Ordered by where the value comes from: the three the tenant must copy
        # out of Exotel's API Settings page first, then the SIP wiring, which is
        # prefilled and which most tenants should not have to touch.
        setup_fields=[
            TelephonySetupField(
                key="account_sid",
                label="Account SID",
                type="text",
                target="account_info",
            ),
            TelephonySetupField(
                key="api_key",
                label="API Key",
                type="secret_ref",
                target="credentials",
                placeholder="API key from Exotel",
                hint="Paste the API key. Stored as an encrypted secret.",
            ),
            TelephonySetupField(
                key="api_token",
                label="API Token",
                type="secret_ref",
                target="credentials",
                placeholder="API token from Exotel",
                hint="Paste the API token. Stored as an encrypted secret.",
            ),
            # Region and subdomain are shown together with the Account SID in the
            # console. They default to Mumbai because that is where Exotel's
            # Indian accounts live and almost every tenant here is one — but they
            # stay editable and are still copied across verbatim rather than one
            # being translated into the other, because that translation is what
            # silently sent a Singapore account to Mumbai.
            TelephonySetupField(
                key="region",
                label="Account region",
                type="text",
                target="account_info",
                default="Mumbai",
                placeholder="Singapore",
                hint="As shown in the Exotel console. Recorded for reference only.",
            ),
            TelephonySetupField(
                key="subdomain",
                label="Subdomain",
                type="text",
                target="account_info",
                default="api.in.exotel.com",
                placeholder="api.exotel.com",
                hint=(
                    "As shown in the Exotel console — this is the API host. "
                    "Wrong subdomain reads as 'Authorization failed', not as a region error."
                ),
            ),
            # Not a per-account value, despite reading like one: Exotel publishes
            # a fixed FQDN per region and auth mode, and the console shows none of
            # them. `in.voip.exotel.com` is the row their Network & Firewall page
            # marks "Specifically used for SIP Auth", which is the mode we use —
            # we authenticate outbound with digest credentials and push no IP ACL.
            # The `edge.*.exotel.com` hosts on that same page are the IP-allowlist
            # path and will not accept our INVITEs.
            #
            # Only India is documented there. A Singapore account has to clear
            # this and get the host from Exotel; `voip.exotel.com` resolves into
            # AWS Singapore but appears in no Exotel document, so it is a guess
            # and is deliberately not offered here.
            TelephonySetupField(
                key="outbound_edge_host",
                label="Outbound SIP edge host",
                type="text",
                target="account_info",
                required=False,
                default="in.voip.exotel.com",
                placeholder="in.voip.exotel.com",
                hint=(
                    "Exotel's outbound SIP edge for India, from their Network & Firewall "
                    "Configuration page — it is not shown anywhere in the Exotel console. "
                    "Leave as is unless your account is outside India."
                ),
            ),
            TelephonySetupField(
                key="outbound_edge_port",
                label="Outbound SIP edge port",
                type="text",
                target="account_info",
                required=False,
                default="443",
                placeholder="443",
                hint="443 for TLS, 5070 for TCP.",
            ),
            TelephonySetupField(
                key="outbound_transport",
                label="Outbound SIP transport",
                type="text",
                target="account_info",
                required=False,
                default="tls",
                placeholder="tls",
                hint="tls (default) or tcp. Exotel does not support UDP.",
            ),
        ],
    ),
    "twilio": TelephonyProviderSpec(
        provider="twilio",
        label="Twilio",
        description="BYO Twilio Elastic SIP Trunking for inbound and outbound PSTN calls.",
        logo_url=_LOGOS["twilio"],
        account_info_keys=["account_sid"],
        credential_keys=["auth_token"],
        implemented=True,
        # Twilio needs `TransferMode=enable-all` on the trunk, which
        # ``TwilioAdapter.ensure_account_sip`` sets over the API on the trunk we
        # create ourselves — so there is no console step and no per-account
        # state. Untested against a live Twilio trunk, and on the bridge for now
        # with the rest; see the note below.
        supports_refer=False,
        setup_fields=[
            # Twilio shows both on the console home page, and the API host is
            # the same worldwide — so unlike Exotel there is no region or
            # subdomain to copy across.
            TelephonySetupField(
                key="account_sid",
                label="Account SID",
                type="text",
                target="account_info",
                placeholder="ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
                hint="From the Twilio console home page.",
            ),
            TelephonySetupField(
                key="auth_token",
                label="Auth Token",
                type="secret_ref",
                target="credentials",
                placeholder="Auth token from Twilio console",
                hint="Paste the auth token. Talqing stores it as an encrypted secret.",
            ),
        ],
    ),
    "vobiz": TelephonyProviderSpec(
        provider="vobiz",
        label="Vobiz",
        description="BYO Vobiz SIP trunking for inbound and outbound PSTN calls.",
        logo_url=_LOGOS["vobiz"],
        account_info_keys=["auth_id"],
        credential_keys=["auth_token"],
        implemented=True,
        # Vobiz documents RFC 3515 REFER and ships a LiveKit transfer example
        # building the same `sip:<e164>@<trunk domain>` URI as Plivo:
        # https://vobiz.ai/docs/examples/vobiz-livekit-call-transfer-example
        #
        # Inbound transfers work. Outbound ones are *accepted* (202) and then
        # never placed — Vobiz's CDRs show a second leg to the original callee
        # ending in `Network Error` and no leg to the transfer target at all.
        # On the bridge for now with the rest; see the note below.
        supports_refer=False,
        setup_fields=[
            # Vobiz serves every account from one global API host, so unlike
            # Exotel there is no region or subdomain to copy across.
            TelephonySetupField(
                key="auth_id",
                label="Auth ID",
                type="text",
                target="account_info",
                placeholder="MA_XXXXXXXX",
                hint="From the Vobiz console home page.",
            ),
            TelephonySetupField(
                key="auth_token",
                label="Auth Token",
                type="secret_ref",
                target="credentials",
                placeholder="Auth token from Vobiz console",
                hint="Paste the auth token. Talqing stores it as an encrypted secret.",
            ),
        ],
    ),
}


def get_provider_spec(provider: str) -> TelephonyProviderSpec:
    spec = TELEPHONY_PROVIDERS.get(provider)
    if spec is None:
        raise KeyError(f"unknown telephony provider: {provider}")
    return spec


def list_provider_specs() -> list[TelephonyProviderSpec]:
    return list(TELEPHONY_PROVIDERS.values())
