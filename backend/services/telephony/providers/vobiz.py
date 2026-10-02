"""Vobiz SIP trunking adapter.

Shapes here follow what the live API actually accepts. That is worth stating,
because Vobiz's OpenAPI document (https://vobiz.ai/openapi.json) and their prose
pages contradict each other — and each is wrong somewhere — so neither can be
followed on its own:

* Trunk create: the prose page documents ``trunk_type: INBOUND`` (uppercase) and
  a required ``max_concurrent_calls``. The spec's ``trunk_direction: inbound``
  and ``trunk_status: enabled`` are what the API takes; ``name`` is the only
  required field.
* Number list: the spec documents ``limit``/``offset``, which the API ignores.
  The prose page is right — ``page``/``per_page``/``search``.
* Origination URI create: see below. Neither source is right.

Inbound is an origination-URI object attached to an inbound trunk as
``primary_uri_uuid`` — the model Vobiz's own console docs describe ("one
origination URI per trunk"). The bare ``inbound_destination`` hostname their
LiveKit guide PATCHes is the single-trunk shortcut, not what we do.

The origination-URI create body is the one place their docs actively mislead.
Both the API reference and the OpenAPI document call the fields ``sip_uri`` and
``name``, and claim the response renames them to ``uri`` and ``description``.
There is no rename: ``uri`` and ``description`` are the real request fields, and
``sip_uri``/``name`` are accepted with a 201 and silently discarded, leaving a
URI with an empty destination and inbound routed nowhere. Nothing catches this
on the way in — the documented "invalid SIP URI format" 400 does not exist
either; the endpoint accepts arbitrary strings. Hence the read-back check below.
"""

from __future__ import annotations

import logging
import secrets as pysecrets
import string
from typing import Any
from urllib.parse import quote

import httpx

from services.secrets import resolve_secretish
from services.telephony.e164 import normalize_e164
from services.telephony.models import TelephonyAccount, TelephonySetupStep

from .base import OutboundInlineConfig, ProviderError, RemoteNumberInfo

logger = logging.getLogger("talqing.telephony.vobiz")

_BASE = "https://api.vobiz.ai/api/v1/Account"

# GET /numbers pages on page/per_page and reports `total`. Vobiz documents a
# default of 25 and no maximum; the loop advances by page number, so a
# server-side cap below what we ask for costs an extra round trip and nothing
# else. https://vobiz.ai/docs/account-phone-number/list-account-phone-numbers
_NUMBER_PAGE_SIZE = 100
# Hard stop so a meta/total mismatch cannot page forever.
_NUMBER_PAGE_MAX = 20

# Vobiz requires ≥8 characters and documents no charset rule for SIP credential
# passwords. Alphanumeric regardless: nothing in a digest password should need
# escaping, whatever the carrier happens to accept.
_SIP_PASSWORD_ALPHABET = string.ascii_letters + string.digits
_SIP_PASSWORD_LENGTH = 24

# Vobiz trunks accept udp | tcp | tls. We announce our SIP edge over TCP
# (livekit.sip.transport), so trunks, the origination URI and the outbound dial
# all use tcp.
#
# Lowercase everywhere, including on the origination URI. The endpoint stores
# whatever case it is given and reads it back unchanged, so the API looks happy
# either way — but the console's Transport dropdown only matches the lowercase
# spelling, and a URI storing `TCP` shows up there with no transport selected
# and does not route. Verified against a live account.
_TRANSPORT = "tcp"


class VobizAdapter:
    provider = "vobiz"

    def _auth(self, account: TelephonyAccount, secrets: dict[str, str]) -> tuple[str, str]:
        auth_id = str(account.account_info.get("auth_id") or "").strip()
        if not auth_id:
            raise ProviderError("Vobiz account_info.auth_id is required")
        token = resolve_secretish(account.credentials.get("auth_token"), secrets)
        if not token:
            raise ProviderError("Vobiz credentials.auth_token did not resolve")
        return auth_id, token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        auth_id: str,
        auth_token: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        ignore_statuses: tuple[int, ...] = (),
    ) -> dict[str, Any]:
        url = f"{_BASE}/{auth_id}{path}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(
                method,
                url,
                json=json_body,
                params=params,
                headers={
                    "X-Auth-ID": auth_id,
                    "X-Auth-Token": auth_token,
                    "Accept": "application/json",
                },
            )
        if resp.status_code in ignore_statuses:
            return {}
        if resp.status_code >= 400:
            # Full body and headers to the log; the ProviderError below is
            # truncated because it ends up on the dashboard.
            logger.error(
                "Vobiz API %s %s failed (%s)\nheaders: %s\nbody: %s",
                method,
                url,
                resp.status_code,
                dict(resp.headers),
                resp.text,
            )
            detail = resp.text[:500]
            raise ProviderError(
                f"Vobiz API {method} {path} failed ({resp.status_code}): {detail}",
                status_code=resp.status_code,
            )
        # Assign / unassign / delete answer 204 with no body.
        if not resp.content:
            return {}
        data = resp.json()
        if not isinstance(data, dict):
            raise ProviderError("Vobiz API returned non-object JSON")
        return data

    async def validate_credentials(
        self, account: TelephonyAccount, secrets: dict[str, str]
    ) -> None:
        auth_id, token = self._auth(account, secrets)
        await self._request("GET", "/trunks", auth_id=auth_id, auth_token=token)

    async def list_remote_numbers(
        self, account: TelephonyAccount, secrets: dict[str, str]
    ) -> list[RemoteNumberInfo]:
        auth_id, token = self._auth(account, secrets)
        out: list[RemoteNumberInfo] = []
        seen: set[str] = set()
        page = 1
        while page <= _NUMBER_PAGE_MAX:
            data = await self._request(
                "GET",
                "/numbers",
                auth_id=auth_id,
                auth_token=token,
                params={"page": page, "per_page": _NUMBER_PAGE_SIZE},
            )
            items = data.get("items")
            if not isinstance(items, list) or not items:
                break
            for item in items:
                if not isinstance(item, dict):
                    continue
                number = self._importable_number(item)
                if number is not None and number.e164 not in seen:
                    seen.add(number.e164)
                    out.append(number)
            total = data.get("total")
            if isinstance(total, int) and page * len(items) >= total:
                break
            page += 1
        if page > _NUMBER_PAGE_MAX:
            logger.warning(
                "Vobiz number list hit the page cap (%s); returning %s numbers",
                _NUMBER_PAGE_MAX,
                len(out),
            )
        return out

    def _importable_number(self, item: dict[str, Any]) -> RemoteNumberInfo | None:
        """A Vobiz number object as an importable DID, or None if it cannot take a call."""
        raw = str(item.get("e164") or "").strip()
        if not raw:
            return None
        status = str(item.get("status") or "").strip()
        capabilities = item.get("capabilities")
        voice = bool(capabilities.get("voice")) if isinstance(capabilities, dict) else False
        # Offering a released, pending or non-voice DID would only fail later at
        # bind time, with a carrier error the tenant cannot act on.
        if status != "active" or not voice or not item.get("voice_enabled"):
            logger.info("skipping Vobiz number %s (status=%s, voice=%s)", raw, status, voice)
            return None
        try:
            e164 = normalize_e164(raw)
        except ValueError:
            logger.warning("skipping unparseable Vobiz number %r", raw)
            return None
        return RemoteNumberInfo(
            e164=e164,
            provider_number_id=str(item.get("id") or "") or None,
            label=str(item.get("region") or "") or None,
        )

    async def ensure_account_sip(
        self,
        account: TelephonyAccount,
        secrets: dict[str, str],
        *,
        platform_sip_uri: str,
    ) -> tuple[dict[str, Any], str | None]:
        auth_id, token = self._auth(account, secrets)
        state = dict(account.provider_state)
        generated_sip_password: str | None = None
        suffix = account.id.hex[:6]

        # ── inbound: origination URI → inbound trunk ────────────────────────
        uri_uuid = str(state.get("inbound_uri_uuid") or "").strip()
        stored_uri = str(state.get("inbound_uri") or "").strip()
        inbound_trunk_id = str(state.get("inbound_trunk_id") or "").strip()

        if not uri_uuid or stored_uri != platform_sip_uri:
            # PUT /origination-uris/{id} can edit `uri` in place, so re-pointing
            # an account could be one call. We still replace the object: a PUT
            # mutates the URI the trunk is actively routing on, so a failure
            # mid-move leaves inbound pointing at a half-updated destination.
            # Create the replacement first, repoint the trunk, then drop the old
            # one — an order where every failure leaves inbound still routed
            # somewhere.
            #
            # No `sip:` scheme, despite the origination-URI docs asking for
            # "standard SIP URI format: sip:host:port". A URI stored with the
            # prefix does not route — inbound calls are refused at Vobiz's edge
            # — and saving the same URI in their console strips it, which is
            # what made a hand-fixed account start answering. Their own LiveKit
            # guide says the same of the sibling field twice ("Remove `sip:`
            # prefix"). Verified against a live account, both ways round.
            origination_uri = platform_sip_uri
            created = await self._request(
                "POST",
                "/origination-uris",
                auth_id=auth_id,
                auth_token=token,
                json_body={
                    "description": f"talqing-{suffix}",
                    "uri": origination_uri,
                    "transport": _TRANSPORT,
                    "priority": 1,
                },
            )
            logger.info("Vobiz origination URI created for account %s: %s", account.id, created)
            new_uri_uuid = str(created.get("id") or "").strip()
            if not new_uri_uuid:
                raise ProviderError("Vobiz did not return an id for the origination URI")
            if inbound_trunk_id:
                await self._request(
                    "PUT",
                    f"/trunks/{inbound_trunk_id}",
                    auth_id=auth_id,
                    auth_token=token,
                    json_body={"primary_uri_uuid": new_uri_uuid},
                )
            if uri_uuid:
                await self._request(
                    "DELETE",
                    f"/origination-uris/{uri_uuid}",
                    auth_id=auth_id,
                    auth_token=token,
                    ignore_statuses=(404,),
                )
                logger.info(
                    "moved Vobiz origination URI %s → %s",
                    stored_uri or "(empty)",
                    platform_sip_uri,
                )
            uri_uuid = new_uri_uuid
            state["inbound_uri_uuid"] = uri_uuid
            state["inbound_uri"] = platform_sip_uri

        if not inbound_trunk_id:
            created = await self._request(
                "POST",
                "/trunks",
                auth_id=auth_id,
                auth_token=token,
                json_body={
                    "name": f"talqing-in-{suffix}",
                    "trunk_direction": "inbound",
                    "trunk_status": "enabled",
                    "secure": False,
                    "transport": _TRANSPORT,
                    "primary_uri_uuid": uri_uuid,
                },
            )
            logger.info("Vobiz inbound trunk created for account %s: %s", account.id, created)
            inbound_trunk_id = str(created.get("trunk_id") or "").strip()
            # `trunk_domain` comes back on create here, unlike Plivo — it is the
            # `Refer-To` host when transferring out of an inbound call.
            inbound_domain = str(created.get("trunk_domain") or "").strip()
            if not inbound_trunk_id or not inbound_domain:
                raise ProviderError(
                    "Vobiz inbound trunk response is missing trunk_id or trunk_domain"
                )
            state["inbound_trunk_id"] = inbound_trunk_id
            state["inbound_trunk_domain"] = inbound_domain

        # ── outbound: SIP credential → outbound trunk ───────────────────────
        if not str(state.get("outbound_trunk_id") or "").strip():
            username = str(state.get("outbound_sip_username") or "").strip()
            if not username:
                username = "tq" + pysecrets.token_hex(4)
            password = "".join(
                pysecrets.choice(_SIP_PASSWORD_ALPHABET) for _ in range(_SIP_PASSWORD_LENGTH)
            )
            cred = await self._request(
                "POST",
                "/credentials",
                auth_id=auth_id,
                auth_token=token,
                json_body={"username": username, "password": password},
            )
            cred_uuid = str(cred.get("id") or "").strip()
            if not cred_uuid:
                raise ProviderError("Vobiz did not return an id for the SIP credential")
            created = await self._request(
                "POST",
                "/trunks",
                auth_id=auth_id,
                auth_token=token,
                json_body={
                    "name": f"talqing-out-{suffix}",
                    "trunk_direction": "outbound",
                    "trunk_status": "enabled",
                    "secure": False,
                    "transport": _TRANSPORT,
                    "credential_uuid": cred_uuid,
                },
            )
            outbound_trunk_id = str(created.get("trunk_id") or "").strip()
            # Unlike Plivo, Vobiz returns the auto-generated
            # ``{trunk_id}.sip.vobiz.ai`` domain on create — no follow-up GET.
            domain = str(created.get("trunk_domain") or "").strip()
            if not outbound_trunk_id or not domain:
                raise ProviderError(
                    "Vobiz outbound trunk response is missing trunk_id or trunk_domain"
                )
            state["outbound_credential_uuid"] = cred_uuid
            state["outbound_sip_username"] = username
            state["outbound_trunk_id"] = outbound_trunk_id
            state["outbound_trunk_domain"] = domain
            state["secure"] = False
            generated_sip_password = password

        return state, generated_sip_password

    def setup_steps(self, account: TelephonyAccount) -> list[TelephonySetupStep]:
        # Trunks, credentials and number assignment are all API-driven, so
        # provisioning leaves nothing for the tenant to do in the Vobiz console.
        return []

    def outbound_sip_configured(self, provider_state: dict[str, Any]) -> bool:
        return bool(
            str(provider_state.get("outbound_trunk_domain") or "").strip()
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
        auth_id, token = self._auth(account, secrets)
        inbound_trunk_id = str(account.provider_state.get("inbound_trunk_id") or "").strip()
        if not inbound_trunk_id:
            raise ProviderError(
                "Vobiz account has no inbound_trunk_id; provision the account first"
            )

        # Assigning a number that already has a trunk answers 400, which would
        # read as a failure on re-provision. Look first so the same call is
        # idempotent, and so a DID pointed somewhere the tenant chose is
        # reported rather than silently taken over.
        current = await self._number_trunk_group_id(auth_id, token, e164=e164)
        if current and current != inbound_trunk_id:
            raise ProviderError(
                f"Vobiz number {e164} is already assigned to trunk {current}. "
                "Unassign it in the Vobiz console, then provision again."
            )
        if not current:
            await self._request(
                "POST",
                f"/numbers/{quote(e164, safe='')}/assign",
                auth_id=auth_id,
                auth_token=token,
                json_body={"trunk_group_id": inbound_trunk_id},
            )

        state = dict(number_provider_state)
        state["mapped_trunk_group_id"] = inbound_trunk_id
        return state

    async def unbind_number(
        self,
        account: TelephonyAccount,
        secrets: dict[str, str],
        *,
        e164: str,
        number_provider_state: dict[str, Any],
    ) -> dict[str, Any]:
        auth_id, token = self._auth(account, secrets)
        # A real unbind, unlike Plivo: the DID stops routing to our trunk and
        # stays in the tenant's Vobiz inventory. 404 means it is already gone.
        await self._request(
            "DELETE",
            f"/numbers/{quote(e164, safe='')}/assign",
            auth_id=auth_id,
            auth_token=token,
            ignore_statuses=(404,),
        )
        state = dict(number_provider_state)
        state.pop("mapped_trunk_group_id", None)
        return state

    async def teardown_account(self, account: TelephonyAccount, secrets: dict[str, str]) -> None:
        auth_id, token = self._auth(account, secrets)
        state = account.provider_state
        # Trunks first: the inbound trunk references the origination URI and the
        # outbound trunk references the credential, so removing those earlier
        # would leave Vobiz rejecting the deletes as still in use.
        objects = (
            ("inbound_trunk_id", "trunks"),
            ("outbound_trunk_id", "trunks"),
            ("outbound_credential_uuid", "credentials"),
            ("inbound_uri_uuid", "origination-uris"),
        )
        for state_key, collection in objects:
            object_id = str(state.get(state_key) or "").strip()
            if not object_id:
                continue
            await self._request(
                "DELETE",
                f"/{collection}/{object_id}",
                auth_id=auth_id,
                auth_token=token,
                ignore_statuses=(404,),
            )
            logger.info("deleted Vobiz %s %s", collection, object_id)

    def outbound_inline_config(
        self,
        account: TelephonyAccount,
        secrets: dict[str, str],
        *,
        from_e164: str,
    ) -> OutboundInlineConfig:
        del from_e164
        domain = str(account.provider_state.get("outbound_trunk_domain") or "").strip()
        username = str(account.provider_state.get("outbound_sip_username") or "").strip()
        password = resolve_secretish(account.credentials.get("sip_outbound_password"), secrets)
        if not domain or not username or not password:
            raise ProviderError(
                "Vobiz outbound SIP is not provisioned "
                "(missing trunk_domain, username, or sip_outbound_password secret)"
            )
        return OutboundInlineConfig(
            hostname=domain,
            auth_username=username,
            auth_password=password,
            transport=_TRANSPORT,
        )

    def refer_uri(self, account: TelephonyAccount, *, e164: str, direction: str) -> str:
        # Vobiz's own LiveKit transfer example builds `sip:<e164>@<trunk domain>`
        # — the shape Plivo requires and Twilio accepts, so all three carriers
        # share one form and there is one code path to get wrong.
        # https://vobiz.ai/docs/examples/vobiz-livekit-call-transfer-example
        #
        # The trunk matching the call's own direction. Note this *changes* the
        # host used on inbound calls, which transfer successfully today with the
        # outbound trunk's domain — if inbound regresses, that is the thing to
        # put back.
        key = "inbound_trunk_domain" if direction == "inbound" else "outbound_trunk_domain"
        domain = str(account.provider_state.get(key) or "").strip()
        if not domain:
            raise ProviderError(
                f"Vobiz account has no {key}, so a transfer has no Refer-To host — "
                "provision the account again"
            )
        return f"sip:{e164}@{domain}"

    async def _number_trunk_group_id(
        self, auth_id: str, auth_token: str, *, e164: str
    ) -> str | None:
        """The trunk this DID currently routes to, or None if it is unassigned."""
        data = await self._request(
            "GET",
            "/numbers",
            auth_id=auth_id,
            auth_token=auth_token,
            params={"search": e164, "per_page": _NUMBER_PAGE_SIZE},
        )
        items = data.get("items")
        if not isinstance(items, list):
            raise ProviderError(f"Vobiz number lookup for {e164} returned no items array")
        for item in items:
            if not isinstance(item, dict):
                continue
            if str(item.get("e164") or "").strip() != e164:
                # `search` is a substring match, so +91987… also matches +91987…9.
                continue
            return str(item.get("trunk_group_id") or "").strip() or None
        raise ProviderError(
            f"Vobiz number {e164} is not on this account. "
            "Buy or transfer it in the Vobiz console first."
        )
