"""Twilio Elastic SIP Trunking adapter.

Twilio splits this integration across two API hosts, and the split matters:

- ``https://trunking.twilio.com/v1`` owns the trunk and everything attached to
  it — origination URLs (inbound), credential-list associations, and the
  number↔trunk mapping.
- ``https://api.twilio.com/2010-04-01/Accounts/{AccountSid}`` owns the account's
  own objects — the SIP credential list we authenticate outbound with, and the
  DID inventory (``IncomingPhoneNumbers``).

Both take ``application/x-www-form-urlencoded`` bodies and answer JSON, and both
authenticate with HTTP basic ``(account_sid, auth_token)``.

References:
- https://www.twilio.com/docs/sip-trunking — trunk model, termination URI rules,
  origination URI transport parameter, authentication.
- https://www.twilio.com/docs/sip-trunking/api — Trunk / OriginationUrl /
  CredentialList / PhoneNumber sub-resources.
- https://www.twilio.com/docs/voice/sip/api/sip-credential-resource — the
  password rules enforced in ``_generate_twilio_sip_password``.
- https://docs.livekit.io/telephony/start/providers/twilio/ — the origination
  URL shape LiveKit expects (``sip:<host>;transport=tcp``).

Every Twilio docs page is also served as raw markdown by appending ``.md`` to
its URL, which is the form to read when checking any of the above.
"""

from __future__ import annotations

import logging
import secrets as pysecrets
import string
from typing import Any

import httpx

from services.secrets import resolve_secretish
from services.telephony.e164 import normalize_e164
from services.telephony.models import TelephonyAccount, TelephonySetupStep

from .base import OutboundInlineConfig, ProviderError, RemoteNumberInfo

logger = logging.getLogger("talqing.telephony.twilio")

_TRUNKING = "https://trunking.twilio.com/v1"
_API = "https://api.twilio.com/2010-04-01"

# Both APIs default to 50 per page and cap at 1000. Lists are walked by
# following the link Twilio returns, never by incrementing a page number:
# ``Page`` is documented as "simply for client state".
_PAGE_SIZE = 200
# Hard stop so a next-page link that never clears cannot page forever.
_PAGE_MAX = 20

# Twilio rejects a SIP credential password unless it is at least 12 characters,
# contains a digit, and mixes case. Generated to satisfy that by construction
# rather than by retrying until Twilio happens to accept one.
_SIP_PASSWORD_LENGTH = 24
_SIP_PASSWORD_ALPHABET = string.ascii_letters + string.digits

# Twilio advertises udp, tcp and tls for both directions (NAPTR on
# pstn.<edge>.twilio.com). We announce our SIP edge over TCP, so the
# origination URI and the outbound dial both say tcp and the trunk stays
# non-secure — Secure Trunking would force TLS+SRTP on both legs and reject
# everything else.
_TRANSPORT = "tcp"

# Twilio requires the termination domain to end here, and it is unique across
# all of Twilio — not just this account. The random suffix is what makes a
# collision self-heal: a retry picks a different name.
_TERMINATION_DOMAIN_SUFFIX = "pstn.twilio.com"

# Trunk-level call transfer (SIP REFER). `enable-all` rather than `sip-only`
# because a transfer destination is a phone number, which is a PSTN transfer;
# `sip-only` would refuse every transfer this platform can express.
# https://www.twilio.com/docs/sip-trunking/api/trunk-resource
_TRANSFER_MODE = "enable-all"
_TRANSFER_CALLER_ID = "from-transferee"


def _generate_twilio_sip_password() -> str:
    """Generate a credential password that always satisfies Twilio's rules.

    ``secrets.token_urlsafe`` is unsuitable: it can come back with no digit, or
    in a single case, both of which Twilio rejects.
    """
    chars = [
        pysecrets.choice(string.ascii_lowercase),
        pysecrets.choice(string.ascii_uppercase),
        pysecrets.choice(string.digits),
    ]
    chars.extend(
        pysecrets.choice(_SIP_PASSWORD_ALPHABET) for _ in range(_SIP_PASSWORD_LENGTH - len(chars))
    )
    pysecrets.SystemRandom().shuffle(chars)
    return "".join(chars)


class TwilioAdapter:
    provider = "twilio"

    def _auth(self, account: TelephonyAccount, secrets: dict[str, str]) -> tuple[str, str]:
        account_sid = str(account.account_info.get("account_sid") or "").strip()
        if not account_sid:
            raise ProviderError("Twilio account_info.account_sid is required")
        token = resolve_secretish(account.credentials.get("auth_token"), secrets)
        if not token:
            raise ProviderError("Twilio credentials.auth_token did not resolve")
        return account_sid, token

    async def _request(
        self,
        method: str,
        url: str,
        *,
        account_sid: str,
        auth_token: str,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        ignore_statuses: tuple[int, ...] = (),
    ) -> dict[str, Any]:
        """One call against either Twilio host. Bodies are form-encoded."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(
                method,
                url,
                auth=(account_sid, auth_token),
                data=data,
                params=params,
                headers={"Accept": "application/json"},
            )
        if resp.status_code in ignore_statuses:
            return {}
        if resp.status_code >= 400:
            # Full body and headers to the log; the ProviderError below is
            # truncated because it ends up on the dashboard. Twilio's body
            # carries a numeric code and a more_info link, both worth having.
            logger.error(
                "Twilio API %s %s failed (%s)\nheaders: %s\nbody: %s",
                method,
                url,
                resp.status_code,
                dict(resp.headers),
                resp.text,
            )
            detail = resp.text[:500]
            if resp.status_code == 401:
                detail = (
                    f"{detail} — check the Account SID and Auth Token, both of which "
                    "are on the Twilio console home page"
                )
            raise ProviderError(
                f"Twilio API {method} {url} failed ({resp.status_code}): {detail}",
                status_code=resp.status_code,
            )
        # Delete answers 204 with no body.
        if not resp.content:
            return {}
        data_out = resp.json()
        if not isinstance(data_out, dict):
            raise ProviderError("Twilio API returned non-object JSON")
        return data_out

    async def validate_credentials(
        self, account: TelephonyAccount, secrets: dict[str, str]
    ) -> None:
        account_sid, token = self._auth(account, secrets)
        # Fetching the account itself, rather than listing trunks: the trunking
        # host derives the account from the credentials, so it would answer 200
        # for a mistyped account_sid — and every other call here puts that sid
        # in the path. This also surfaces a suspended account now instead of at
        # the first call attempt.
        info = await self._request(
            "GET",
            f"{_API}/Accounts/{account_sid}.json",
            account_sid=account_sid,
            auth_token=token,
        )
        status = str(info.get("status") or "").strip().lower()
        if status and status != "active":
            raise ProviderError(
                f"Twilio account {account_sid} is {status}, not active. "
                "Resolve it in the Twilio console before connecting it here."
            )

    async def list_remote_numbers(
        self, account: TelephonyAccount, secrets: dict[str, str]
    ) -> list[RemoteNumberInfo]:
        account_sid, token = self._auth(account, secrets)
        out: list[RemoteNumberInfo] = []
        seen: set[str] = set()
        url = f"{_API}/Accounts/{account_sid}/IncomingPhoneNumbers.json"
        params: dict[str, Any] | None = {"PageSize": _PAGE_SIZE}
        for _ in range(_PAGE_MAX):
            data = await self._request(
                "GET", url, account_sid=account_sid, auth_token=token, params=params
            )
            items = data.get("incoming_phone_numbers")
            if not isinstance(items, list):
                raise ProviderError(
                    "Twilio IncomingPhoneNumbers response has no "
                    "'incoming_phone_numbers' list; the API shape may have changed"
                )
            for item in items:
                if not isinstance(item, dict):
                    continue
                number = self._importable_number(item)
                if number is not None and number.e164 not in seen:
                    seen.add(number.e164)
                    out.append(number)
            # next_page_uri is a path relative to api.twilio.com and already
            # carries the paging cursor, so the query params must not be resent.
            next_uri = str(data.get("next_page_uri") or "").strip()
            if not next_uri:
                return out
            url = f"https://api.twilio.com{next_uri}"
            params = None
        logger.warning(
            "Twilio number list hit the page cap (%s); returning %s numbers",
            _PAGE_MAX,
            len(out),
        )
        return out

    def _importable_number(self, item: dict[str, Any]) -> RemoteNumberInfo | None:
        """A Twilio number as an importable DID, or None if it cannot take a call."""
        raw = str(item.get("phone_number") or "").strip()
        if not raw:
            return None
        capabilities = item.get("capabilities")
        voice = bool(capabilities.get("voice")) if isinstance(capabilities, dict) else False
        if not voice:
            # An SMS-only DID would only fail later at bind time, with a carrier
            # error the tenant cannot act on.
            logger.info("skipping Twilio number %s (no voice capability)", raw)
            return None
        try:
            e164 = normalize_e164(raw)
        except ValueError:
            logger.warning("skipping unparseable Twilio number %r", raw)
            return None
        return RemoteNumberInfo(
            e164=e164,
            provider_number_id=str(item.get("sid") or "") or None,
            label=str(item.get("friendly_name") or "").strip() or None,
        )

    async def ensure_account_sip(
        self,
        account: TelephonyAccount,
        secrets: dict[str, str],
        *,
        platform_sip_uri: str,
    ) -> tuple[dict[str, Any], str | None]:
        account_sid, token = self._auth(account, secrets)
        state = dict(account.provider_state)
        generated_sip_password: str | None = None
        suffix = account.id.hex[:6]
        # Derived from the account id, so it is the same on every provision —
        # which is what makes the objects below findable again if a provision
        # dies between creating one and storing its sid.
        friendly_name = f"talqing-{suffix}"

        # ── the trunk itself ────────────────────────────────────────────────
        trunk_sid = str(state.get("trunk_sid") or "").strip()
        if not trunk_sid:
            existing = await self._find_trunk(account_sid, token, friendly_name=friendly_name)
            if existing is not None:
                trunk_sid = str(existing.get("sid") or "").strip()
                trunk_domain = str(existing.get("domain_name") or "").strip()
                logger.info("adopted existing Twilio trunk %s (%s)", trunk_sid, friendly_name)
            else:
                # The domain is unique across all of Twilio, not just this
                # account, so it carries random bits: a name that loses the race
                # against another Twilio customer is resolved by provisioning
                # again, which picks a different one.
                domain = (
                    f"tq-{account.id.hex[:8]}-{pysecrets.token_hex(3)}.{_TERMINATION_DOMAIN_SUFFIX}"
                )
                created = await self._request(
                    "POST",
                    f"{_TRUNKING}/Trunks",
                    account_sid=account_sid,
                    auth_token=token,
                    data={
                        "FriendlyName": friendly_name,
                        "DomainName": domain,
                        # Secure Trunking would force TLS+SRTP and reject the
                        # tcp legs configured on both sides of this adapter.
                        "Secure": False,
                        # Call transfer over SIP REFER, set here rather than
                        # asked of the tenant in the console — the console's
                        # "Enable PSTN Transfer" *is* `enable-all` rather than
                        # `sip-only`, and both are ordinary fields on the trunk
                        # we are already creating.
                        "TransferMode": _TRANSFER_MODE,
                        # The human who answers a transfer sees the *caller's*
                        # number, not our DID. It is what a support agent needs,
                        # and it is what Plivo does implicitly and
                        # unconfigurably — so choosing it is what makes the two
                        # REFER carriers behave identically.
                        "TransferCallerId": _TRANSFER_CALLER_ID,
                    },
                )
                trunk_sid = str(created.get("sid") or "").strip()
                trunk_domain = str(created.get("domain_name") or "").strip()
            if not trunk_sid or not trunk_domain:
                raise ProviderError("Twilio trunk response is missing sid or domain_name")
            state["trunk_sid"] = trunk_sid
            state["trunk_domain"] = trunk_domain
            state["trunk_friendly_name"] = friendly_name
            state["secure"] = False
            # What we asked Twilio for, recorded the way `secure` is, so it is
            # inspectable without a carrier round-trip. Not read back and not
            # patched: the only trunk `_find_trunk` ever adopts is one *we*
            # created under our own friendly_name, which carries both fields
            # already, and every other trunk on the account is the tenant's own.
            state["transfer_mode"] = _TRANSFER_MODE
            state["transfer_caller_id"] = _TRANSFER_CALLER_ID

        # ── inbound: origination URL → our SIP edge ─────────────────────────
        # Twilio fills in the user part from the DID being called, so this host
        # alone routes every number on the trunk to us.
        sip_url = f"sip:{platform_sip_uri}"
        origination_sid = str(state.get("origination_url_sid") or "").strip()
        if not origination_sid:
            # A trunk we just adopted already carries the origination URL the
            # lost provision created. Creating a second one would fork inbound
            # across two entries rather than replace the first.
            origination_sid = await self._find_origination_url(
                account_sid, token, trunk_sid=trunk_sid, friendly_name=friendly_name
            )
        if not origination_sid:
            created = await self._request(
                "POST",
                f"{_TRUNKING}/Trunks/{trunk_sid}/OriginationUrls",
                account_sid=account_sid,
                auth_token=token,
                data={
                    "FriendlyName": friendly_name,
                    "SipUrl": sip_url,
                    "Priority": 1,
                    "Weight": 1,
                    "Enabled": True,
                },
            )
            origination_sid = str(created.get("sid") or "").strip()
            if not origination_sid:
                raise ProviderError("Twilio did not return a sid for the origination URL")
            state["origination_url_sid"] = origination_sid
            state["origination_sip_url"] = sip_url
        elif str(state.get("origination_sip_url") or "").strip() != sip_url:
            # LIVEKIT_SIP_URI changed (dev→prod, edge re-point) — Twilio updates
            # an origination URL in place, so inbound never has a gap.
            await self._request(
                "POST",
                f"{_TRUNKING}/Trunks/{trunk_sid}/OriginationUrls/{origination_sid}",
                account_sid=account_sid,
                auth_token=token,
                data={"SipUrl": sip_url, "Enabled": True},
            )
            logger.info(
                "updated Twilio origination URL %s → %s",
                str(state.get("origination_sip_url") or "") or "(empty)",
                sip_url,
            )
            state["origination_url_sid"] = origination_sid
            state["origination_sip_url"] = sip_url

        # ── outbound: credential list → trunk termination auth ──────────────
        if not str(state.get("outbound_sip_username") or "").strip():
            username = "tq" + pysecrets.token_hex(4)
            password = _generate_twilio_sip_password()
            credential_list = await self._request(
                "POST",
                f"{_API}/Accounts/{account_sid}/SIP/CredentialLists.json",
                account_sid=account_sid,
                auth_token=token,
                data={"FriendlyName": friendly_name},
            )
            credential_list_sid = str(credential_list.get("sid") or "").strip()
            if not credential_list_sid:
                raise ProviderError("Twilio did not return a sid for the SIP credential list")
            await self._request(
                "POST",
                f"{_API}/Accounts/{account_sid}/SIP/CredentialLists/"
                f"{credential_list_sid}/Credentials.json",
                account_sid=account_sid,
                auth_token=token,
                data={"Username": username, "Password": password},
            )
            # Termination stays closed until the list is attached to the trunk:
            # a trunk with no credential list and no IP ACL accepts no traffic.
            await self._request(
                "POST",
                f"{_TRUNKING}/Trunks/{trunk_sid}/CredentialLists",
                account_sid=account_sid,
                auth_token=token,
                data={"CredentialListSid": credential_list_sid},
            )
            state["outbound_credential_list_sid"] = credential_list_sid
            state["outbound_sip_username"] = username
            generated_sip_password = password

        return state, generated_sip_password

    def setup_steps(self, account: TelephonyAccount) -> list[TelephonySetupStep]:
        # Trunks, origination URLs, credentials and number association are all
        # API-driven, so provisioning leaves nothing for the tenant to do in the
        # Twilio console.
        return []

    def outbound_sip_configured(self, provider_state: dict[str, Any]) -> bool:
        return bool(
            str(provider_state.get("trunk_domain") or "").strip()
            and str(provider_state.get("outbound_sip_username") or "").strip()
        )

    async def bind_number(
        self,
        account: TelephonyAccount,
        secrets: dict[str, str],
        *,
        e164: str,
        number_provider_state: dict[str, Any],
    ) -> dict[str, Any]:
        account_sid, token = self._auth(account, secrets)
        trunk_sid = str(account.provider_state.get("trunk_sid") or "").strip()
        if not trunk_sid:
            raise ProviderError("Twilio account has no trunk_sid; provision the account first")

        number = await self._incoming_number(account_sid, token, e164=e164)
        number_sid = str(number.get("sid") or "").strip()
        if not number_sid:
            raise ProviderError(f"Twilio number {e164} has no sid")
        current = str(number.get("trunk_sid") or "").strip()
        if current and current != trunk_sid:
            # Associating would silently move the DID off a trunk the tenant
            # chose, so say so instead. Same call is a no-op when it is already
            # ours, which keeps re-provision idempotent.
            raise ProviderError(
                f"Twilio number {e164} is already attached to trunk {current}. "
                "Detach it in the Twilio console, then provision again."
            )
        if not current:
            await self._request(
                "POST",
                f"{_TRUNKING}/Trunks/{trunk_sid}/PhoneNumbers",
                account_sid=account_sid,
                auth_token=token,
                data={"PhoneNumberSid": number_sid},
            )

        state = dict(number_provider_state)
        state["provider_number_sid"] = number_sid
        state["mapped_trunk_sid"] = trunk_sid
        return state

    async def unbind_number(
        self,
        account: TelephonyAccount,
        secrets: dict[str, str],
        *,
        e164: str,
        number_provider_state: dict[str, Any],
    ) -> dict[str, Any]:
        account_sid, token = self._auth(account, secrets)
        state = dict(number_provider_state)
        trunk_sid = str(account.provider_state.get("trunk_sid") or "").strip()
        number_sid = str(state.get("provider_number_sid") or "").strip()
        if not trunk_sid or not number_sid:
            # Never mapped, or mapped before we recorded the sid. Twilio keys
            # the association by the number's sid, so there is nothing to
            # address here.
            return state
        # A real unbind, unlike Plivo: the DID stops routing to our trunk, goes
        # back to its own voice settings, and stays in the tenant's inventory.
        await self._request(
            "DELETE",
            f"{_TRUNKING}/Trunks/{trunk_sid}/PhoneNumbers/{number_sid}",
            account_sid=account_sid,
            auth_token=token,
            ignore_statuses=(404,),
        )
        state.pop("mapped_trunk_sid", None)
        logger.info("detached Twilio number %s (%s) from trunk %s", e164, number_sid, trunk_sid)
        return state

    async def teardown_account(self, account: TelephonyAccount, secrets: dict[str, str]) -> None:
        account_sid, token = self._auth(account, secrets)
        state = account.provider_state
        trunk_sid = str(state.get("trunk_sid") or "").strip()
        credential_list_sid = str(state.get("outbound_credential_list_sid") or "").strip()

        # Revoke the outbound login first, so a failure later cannot strand a
        # working SIP credential on the tenant's account. The credential list is
        # an account-level object the trunk only references, so it has to be
        # detached before it can be deleted.
        if trunk_sid and credential_list_sid:
            await self._request(
                "DELETE",
                f"{_TRUNKING}/Trunks/{trunk_sid}/CredentialLists/{credential_list_sid}",
                account_sid=account_sid,
                auth_token=token,
                ignore_statuses=(404,),
            )
        if credential_list_sid:
            await self._request(
                "DELETE",
                f"{_API}/Accounts/{account_sid}/SIP/CredentialLists/{credential_list_sid}.json",
                account_sid=account_sid,
                auth_token=token,
                ignore_statuses=(404,),
            )
            logger.info("deleted Twilio credential list %s", credential_list_sid)
        if trunk_sid:
            # Deleting the trunk takes its origination URLs and number
            # associations with it; the DIDs return to their own voice settings.
            await self._request(
                "DELETE",
                f"{_TRUNKING}/Trunks/{trunk_sid}",
                account_sid=account_sid,
                auth_token=token,
                ignore_statuses=(404,),
            )
            logger.info("deleted Twilio trunk %s", trunk_sid)

    def outbound_inline_config(
        self,
        account: TelephonyAccount,
        secrets: dict[str, str],
        *,
        from_e164: str,
    ) -> OutboundInlineConfig:
        del from_e164
        domain = str(account.provider_state.get("trunk_domain") or "").strip()
        username = str(account.provider_state.get("outbound_sip_username") or "").strip()
        password = resolve_secretish(account.credentials.get("sip_outbound_password"), secrets)
        if not domain or not username or not password:
            raise ProviderError(
                "Twilio outbound SIP is not provisioned "
                "(missing trunk_domain, username, or sip_outbound_password secret)"
            )
        return OutboundInlineConfig(
            hostname=domain,
            auth_username=username,
            auth_password=password,
            transport=_TRANSPORT,
        )

    def refer_uri(self, account: TelephonyAccount, *, e164: str, direction: str) -> str:
        # One trunk serves both directions on Twilio, so there is no
        # per-direction domain to choose between.
        del direction
        # Twilio accepts `tel:+E164` too, but the `sip:` form on our own
        # termination domain is what Plivo requires, so both carriers take one
        # shape and there is one code path to get wrong.
        # https://www.twilio.com/docs/sip-trunking/call-transfer
        domain = str(account.provider_state.get("trunk_domain") or "").strip()
        if not domain:
            raise ProviderError(
                "Twilio account has no trunk_domain, so a transfer has no "
                "Refer-To host — provision the account first"
            )
        return f"sip:{e164}@{domain}"

    async def _find_trunk(
        self, account_sid: str, auth_token: str, *, friendly_name: str
    ) -> dict[str, Any] | None:
        """The trunk we created for this account, or None if Twilio has none.

        Only reached when our copy of ``trunk_sid`` is missing, which means a
        provision died between creating the trunk and storing its sid.
        """
        url = f"{_TRUNKING}/Trunks"
        params: dict[str, Any] | None = {"PageSize": _PAGE_SIZE}
        for _ in range(_PAGE_MAX):
            data = await self._request(
                "GET", url, account_sid=account_sid, auth_token=auth_token, params=params
            )
            trunks = data.get("trunks")
            if not isinstance(trunks, list):
                raise ProviderError(
                    "Twilio GET /Trunks response has no 'trunks' list, "
                    "so existing trunks cannot be checked"
                )
            for trunk in trunks:
                if isinstance(trunk, dict) and trunk.get("friendly_name") == friendly_name:
                    return trunk
            meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
            next_url = str(meta.get("next_page_url") or "").strip()
            if not next_url:
                return None
            url = next_url
            params = None
        logger.warning("Twilio trunk list hit the page cap (%s) without a match", _PAGE_MAX)
        return None

    async def _find_origination_url(
        self, account_sid: str, auth_token: str, *, trunk_sid: str, friendly_name: str
    ) -> str:
        """Sid of the origination URL we created on this trunk, or "" if absent.

        Not paginated: Twilio caps a trunk at ten origination URIs, so the first
        page always holds all of them.
        """
        data = await self._request(
            "GET",
            f"{_TRUNKING}/Trunks/{trunk_sid}/OriginationUrls",
            account_sid=account_sid,
            auth_token=auth_token,
            params={"PageSize": _PAGE_SIZE},
        )
        urls = data.get("origination_urls")
        if not isinstance(urls, list):
            raise ProviderError(
                "Twilio GET /OriginationUrls response has no 'origination_urls' list, "
                "so inbound routing cannot be checked"
            )
        for item in urls:
            if isinstance(item, dict) and item.get("friendly_name") == friendly_name:
                return str(item.get("sid") or "").strip()
        return ""

    async def _incoming_number(
        self, account_sid: str, auth_token: str, *, e164: str
    ) -> dict[str, Any]:
        """The account's IncomingPhoneNumber for this DID. Raises if it has none."""
        data = await self._request(
            "GET",
            f"{_API}/Accounts/{account_sid}/IncomingPhoneNumbers.json",
            account_sid=account_sid,
            auth_token=auth_token,
            params={"PhoneNumber": e164, "PageSize": _PAGE_SIZE},
        )
        items = data.get("incoming_phone_numbers")
        if not isinstance(items, list):
            raise ProviderError(f"Twilio number lookup for {e164} returned no list")
        for item in items:
            if not isinstance(item, dict):
                continue
            # `PhoneNumber` matches partially and accepts wildcards, so +1415…0
            # also matches +1415…01. Only an exact DID is this number.
            if str(item.get("phone_number") or "").strip() == e164:
                return item
        raise ProviderError(
            f"Twilio number {e164} is not on this account. "
            "Buy or port it in the Twilio console first."
        )
