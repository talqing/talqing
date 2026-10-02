"""Plivo Zentrunk adapter."""

from __future__ import annotations

import logging
import secrets as pysecrets
import string
from typing import Any

import httpx

from services.secrets import resolve_secretish
from services.telephony.e164 import normalize_e164, plivo_number_key
from services.telephony.models import TelephonyAccount, TelephonySetupStep

from .base import OutboundInlineConfig, ProviderError, RemoteNumberInfo

logger = logging.getLogger("talqing.telephony.plivo")

_BASE = "https://api.plivo.com/v1/Account"

# Plivo Number list: max 20 per page (https://www.plivo.com/docs/numbers/api/account-phone-number).
_NUMBER_PAGE_SIZE = 20
# Hard stop so a runaway meta/offset loop cannot page forever.
_NUMBER_LIST_MAX = 2000

# Zentrunk credential password: 5–20 chars, alnum + these specials, ≥1 special.
# https://www.plivo.com/docs/sip-trunking/api/credentials
_PLIVO_PASSWORD_SPECIALS = "~!@#$%^&*()_+"
_PLIVO_PASSWORD_ALPHABET = string.ascii_letters + string.digits + _PLIVO_PASSWORD_SPECIALS


def _generate_plivo_sip_password(*, length: int = 16) -> str:
    """Generate a Zentrunk digest password that always satisfies Plivo's charset rules.

    ``secrets.token_urlsafe`` is unsuitable: base64url includes ``-``, which Plivo rejects.
    """
    if length < 5 or length > 20:
        raise ValueError("Plivo SIP password length must be 5–20")
    # Force at least one allowed special; fill the rest from the full allowed alphabet.
    chars = [pysecrets.choice(_PLIVO_PASSWORD_SPECIALS)]
    chars.extend(pysecrets.choice(_PLIVO_PASSWORD_ALPHABET) for _ in range(length - 1))
    pysecrets.SystemRandom().shuffle(chars)
    return "".join(chars)


class PlivoAdapter:
    provider = "plivo"

    def _auth(self, account: TelephonyAccount, secrets: dict[str, str]) -> tuple[str, str]:
        auth_id = str(account.account_info.get("auth_id") or "").strip()
        if not auth_id:
            raise ProviderError("Plivo account_info.auth_id is required")
        token = resolve_secretish(account.credentials.get("auth_token"), secrets)
        if not token:
            raise ProviderError("Plivo credentials.auth_token did not resolve")
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
                auth=(auth_id, auth_token),
                json=json_body,
                params=params,
                headers={"Accept": "application/json"},
            )
        if resp.status_code in ignore_statuses:
            return {}
        if resp.status_code >= 400:
            # Full body and headers to the log; the ProviderError below is
            # truncated because it ends up on the dashboard.
            logger.error(
                "Plivo API %s %s failed (%s)\nheaders: %s\nbody: %s",
                method,
                url,
                resp.status_code,
                dict(resp.headers),
                resp.text,
            )
            detail = resp.text[:500]
            raise ProviderError(
                f"Plivo API {method} {path} failed ({resp.status_code}): {detail}",
                status_code=resp.status_code,
            )
        if not resp.content:
            return {}
        data = resp.json()
        if not isinstance(data, dict):
            raise ProviderError("Plivo API returned non-object JSON")
        return data

    async def validate_credentials(
        self, account: TelephonyAccount, secrets: dict[str, str]
    ) -> None:
        auth_id, token = self._auth(account, secrets)
        await self._request("GET", "/Zentrunk/Trunk/", auth_id=auth_id, auth_token=token)

    async def list_remote_numbers(
        self, account: TelephonyAccount, secrets: dict[str, str]
    ) -> list[RemoteNumberInfo]:
        auth_id, token = self._auth(account, secrets)
        out: list[RemoteNumberInfo] = []
        seen: set[str] = set()
        offset = 0
        while offset < _NUMBER_LIST_MAX:
            data = await self._request(
                "GET",
                "/Number/",
                auth_id=auth_id,
                auth_token=token,
                params={"limit": _NUMBER_PAGE_SIZE, "offset": offset},
            )
            objects = data.get("objects") or data.get("numbers") or []
            if not isinstance(objects, list) or not objects:
                break
            for item in objects:
                if not isinstance(item, dict):
                    continue
                number = str(item.get("number") or "").strip()
                if not number:
                    continue
                try:
                    e164 = normalize_e164(f"+{number}" if not number.startswith("+") else number)
                except ValueError:
                    logger.warning("skipping unparseable Plivo number %r", number)
                    continue
                if e164 in seen:
                    continue
                seen.add(e164)
                out.append(
                    RemoteNumberInfo(
                        e164=e164,
                        provider_number_id=number,
                        label=str(item.get("alias") or "") or None,
                    )
                )
            page_len = len(objects)
            offset += page_len
            meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
            total = meta.get("total_count")
            if page_len < _NUMBER_PAGE_SIZE:
                break
            if isinstance(total, int) and offset >= total:
                break
        if offset >= _NUMBER_LIST_MAX:
            logger.warning(
                "Plivo number list hit safety cap (%s); returning %s numbers",
                _NUMBER_LIST_MAX,
                len(out),
            )
        return out

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

        uri_uuid = str(state.get("inbound_uri_uuid") or "").strip()
        stored_uri = str(state.get("inbound_uri") or "").strip()
        if not uri_uuid:
            created = await self._request(
                "POST",
                "/Zentrunk/URI/",
                auth_id=auth_id,
                auth_token=token,
                json_body={
                    "name": f"talqing-{account.id.hex[:6]}",
                    "uri": platform_sip_uri,
                },
            )
            uri_uuid = str(created.get("uri_uuid") or "").strip()
            if not uri_uuid:
                raise ProviderError("Plivo did not return uri_uuid for origination URI")
            state["inbound_uri_uuid"] = uri_uuid
            state["inbound_uri"] = platform_sip_uri
        elif stored_uri != platform_sip_uri:
            # LIVEKIT_SIP_URI changed (dev→prod, host rotate) — update origination.
            await self._request(
                "POST",
                f"/Zentrunk/URI/{uri_uuid}/",
                auth_id=auth_id,
                auth_token=token,
                json_body={"uri": platform_sip_uri},
            )
            state["inbound_uri"] = platform_sip_uri
            logger.info(
                "updated Plivo origination URI %s → %s",
                stored_uri or "(empty)",
                platform_sip_uri,
            )

        inbound_trunk_id = str(state.get("inbound_trunk_id") or "").strip()
        if not inbound_trunk_id:
            created = await self._request(
                "POST",
                "/Zentrunk/Trunk/",
                auth_id=auth_id,
                auth_token=token,
                json_body={
                    "name": f"talqing-in-{account.id.hex[:6]}",
                    "trunk_direction": "inbound",
                    "trunk_status": "enabled",
                    "secure": False,
                    "primary_uri_uuid": uri_uuid,
                },
            )
            inbound_trunk_id = str(created.get("trunk_id") or "").strip()
            if not inbound_trunk_id:
                raise ProviderError("Plivo did not return inbound trunk_id")
            # A second call because Plivo's trunk-create response carries only
            # api_id/message/trunk_id, exactly as the outbound trunk below has
            # to do. Needed for the `Refer-To` when transferring out of an
            # inbound call. Retrieve wraps the trunk in `object`, unlike create.
            detail = await self._request(
                "GET",
                f"/Zentrunk/Trunk/{inbound_trunk_id}/",
                auth_id=auth_id,
                auth_token=token,
            )
            obj = detail.get("object") if isinstance(detail.get("object"), dict) else detail
            inbound_domain = str(obj.get("trunk_domain") or "").strip()
            if not inbound_domain:
                raise ProviderError("Plivo inbound trunk has no trunk_domain")
            state["inbound_trunk_id"] = inbound_trunk_id
            state["inbound_trunk_domain"] = inbound_domain

        outbound_trunk_id = str(state.get("outbound_trunk_id") or "").strip()
        if not outbound_trunk_id:
            username = str(state.get("outbound_sip_username") or "").strip()
            if not username:
                username = "tq" + pysecrets.token_hex(4)
            password = _generate_plivo_sip_password()
            cred = await self._request(
                "POST",
                "/Zentrunk/Credential/",
                auth_id=auth_id,
                auth_token=token,
                json_body={
                    "name": f"talqing-out-{account.id.hex[:6]}",
                    "username": username,
                    "password": password,
                },
            )
            cred_uuid = str(cred.get("credential_uuid") or "").strip()
            if not cred_uuid:
                raise ProviderError("Plivo did not return credential_uuid")
            created = await self._request(
                "POST",
                "/Zentrunk/Trunk/",
                auth_id=auth_id,
                auth_token=token,
                json_body={
                    "name": f"talqing-out-{account.id.hex[:6]}",
                    "trunk_direction": "outbound",
                    "trunk_status": "enabled",
                    "credential_uuid": cred_uuid,
                    "secure": False,
                },
            )
            outbound_trunk_id = str(created.get("trunk_id") or "").strip()
            if not outbound_trunk_id:
                raise ProviderError("Plivo did not return outbound trunk_id")
            detail = await self._request(
                "GET",
                f"/Zentrunk/Trunk/{outbound_trunk_id}/",
                auth_id=auth_id,
                auth_token=token,
            )
            obj = detail.get("object") if isinstance(detail.get("object"), dict) else detail
            domain = str(obj.get("trunk_domain") or "").strip()
            if not domain:
                raise ProviderError("Plivo outbound trunk has no trunk_domain")
            state["outbound_credential_uuid"] = cred_uuid
            state["outbound_sip_username"] = username
            state["outbound_trunk_id"] = outbound_trunk_id
            state["outbound_trunk_domain"] = domain
            state["secure"] = False
            generated_sip_password = password

        return state, generated_sip_password

    def setup_steps(self, account: TelephonyAccount) -> list[TelephonySetupStep]:
        # Zentrunk trunks, credentials and number mapping are all API-driven, so
        # provisioning leaves nothing for the tenant to do in the Plivo console.
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
                "Plivo account has no inbound_trunk_id; provision the account first"
            )
        key = plivo_number_key(e164)
        await self._request(
            "POST",
            f"/Number/{key}/",
            auth_id=auth_id,
            auth_token=token,
            json_body={"app_id": inbound_trunk_id},
        )
        state = dict(number_provider_state)
        state["mapped_app_id"] = inbound_trunk_id
        state["provider_number_key"] = key
        return state

    async def unbind_number(
        self,
        account: TelephonyAccount,
        secrets: dict[str, str],
        *,
        e164: str,
        number_provider_state: dict[str, Any],
    ) -> dict[str, Any]:
        del account, secrets
        # No-op by necessity. Plivo documents POST /Number/{number}/ {app_id} as
        # an assignment; nothing in the reference clears app_id, and guessing at
        # a value that might mean "none" risks routing the DID somewhere the
        # tenant did not choose. The number keeps pointing at the deleted
        # inbound trunk until it is remapped in the Plivo console.
        mapped = str(number_provider_state.get("mapped_app_id") or "").strip()
        if mapped:
            logger.warning(
                "Plivo %s stays mapped to app_id %s after disable; "
                "Plivo documents no way to unassign a number",
                e164,
                mapped,
            )
        return dict(number_provider_state)

    async def teardown_account(self, account: TelephonyAccount, secrets: dict[str, str]) -> None:
        auth_id, token = self._auth(account, secrets)
        state = account.provider_state
        # Trunks first: the inbound trunk references the origination URI and the
        # outbound trunk references the credential, so removing those earlier
        # would leave Plivo rejecting the deletes as still in use.
        objects = (
            ("inbound_trunk_id", "Zentrunk/Trunk"),
            ("outbound_trunk_id", "Zentrunk/Trunk"),
            ("outbound_credential_uuid", "Zentrunk/Credential"),
            ("inbound_uri_uuid", "Zentrunk/URI"),
        )
        for state_key, collection in objects:
            object_id = str(state.get(state_key) or "").strip()
            if not object_id:
                continue
            await self._request(
                "DELETE",
                f"/{collection}/{object_id}/",
                auth_id=auth_id,
                auth_token=token,
                ignore_statuses=(404,),
            )
            logger.info("deleted Plivo %s %s", collection, object_id)

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
                "Plivo outbound SIP is not provisioned "
                "(missing trunk_domain, username, or sip_outbound_password secret)"
            )
        return OutboundInlineConfig(
            hostname=domain,
            auth_username=username,
            auth_password=password,
            transport="tcp",
        )

    def refer_uri(self, account: TelephonyAccount, *, e164: str, direction: str) -> str:
        # Plivo blocks `tel:` and any foreign SIP domain: the Refer-To host must
        # be one of this account's own Zentrunk domains, which is why a missing
        # key raises here instead of degrading to `tel:` — that would fail at
        # REFER time, mid-call.
        # https://www.plivo.com/docs/sip-trunking/concepts/sip-refer
        #
        # The trunk matching the call's own direction. Plivo's guides show the
        # REFER's Request-URI, To and Refer-To all sharing one domain, and the
        # first two are fixed by the dialog — so the trunk the call is on is the
        # better reading of their unqualified `<trunk-id>.zt.plivo.com`.
        key = "inbound_trunk_domain" if direction == "inbound" else "outbound_trunk_domain"
        domain = str(account.provider_state.get(key) or "").strip()
        if not domain:
            raise ProviderError(
                f"Plivo account has no {key}, so a transfer has no Refer-To host — "
                "provision the account again"
            )
        return f"sip:{e164}@{domain}"
