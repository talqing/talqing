"""Exotel Dynamic SIP Trunking adapter.

Sources, in the order to trust them:

1. https://github.com/exotel/exotel-vsip-trunk-Configuration-API — Exotel's own
   sample repo (curl/python/go/postman). Documents a considerably larger API
   surface than the reference page below, whose "API Endpoints Summary" table
   lists only ten calls. Endpoints that exist here and nowhere else include
   ``GET /trunks`` (paginated, optional ``trunk_sid`` filter),
   ``GET /trunk-maps?exophone=``, ``PUT /trunks/{sid}``, credential
   list/update, and DELETE/PUT for destination-uris, phone-numbers and
   settings. Its CI only syntax-checks the scripts, so treat anything that
   appears solely in the Postman collection as unverified against a live
   account; the curl/python/go scripts are three independent hand-written
   implementations and carry more weight.
2. https://docs.exotel.com/dynamic-sip-trunking/detailed-sip-trunking-api-reference
   — now an index; each call has its own page (``/create-trunk``,
   ``/map-phone-number-to-trunk``, ``/create-credentials-digest-auth-api-for-a-trunk``,
   ``/map-destination-uri-to-trunk``, …). Request/response fields and the
   error-code table (1008 = duplicate) live on those.
3. A live account, for anything the pages above leave ambiguous. Three of the
   values we send were wrong against real trunks while matching the prose:
   ``nso_code`` must be ``ANY-ANY`` (not ``any any``), ``friendly_name`` takes
   no hyphen, and a duplicate trunk is reported inside a ``200 OK`` body.

Note ``docs.exotel.com/dynamic-sip-trunking/connect-exotel-sip-trunk-to-livekit``,
which this module used to cite as the tiebreaker for running digest-only with no
IP ACL, now 404s. That decision is unchanged and still holds — see the comment in
``ensure_account_sip`` — but its source is gone, so re-derive rather than
re-cite if it is ever questioned.

Both docs sites are client-rendered: fetching them needs the ``<script>``
payload, not the stripped HTML.
"""

from __future__ import annotations

import logging
import secrets as pysecrets
from typing import Any

import httpx

from services.secrets import resolve_secretish
from services.telephony.e164 import normalize_e164
from services.telephony.models import (
    TelephonyAccount,
    TelephonySetupInstruction,
    TelephonySetupStep,
)

from .base import OutboundInlineConfig, ProviderError, RemoteNumberInfo

logger = logging.getLogger("talqing.telephony.exotel")

# Port and transport are region-independent (Exotel: 443 tls, 5070 tcp, no UDP).
# The edge HOST is not, and Exotel shows it nowhere in the console — it is a
# fixed FQDN per region and auth mode, published only in their Network &
# Firewall Configuration page. The catalog prefills the Indian one; there is
# still no default here, because a wrong host fails at the first outbound call,
# long after the mistake, which is worse than refusing to provision.
_DEFAULT_OUTBOUND_EDGE_PORT = 443
_DEFAULT_OUTBOUND_TRANSPORT = "tls"

# Exotel's paginated GETs default to 50 per page and echo next_page_uri while
# more remain (documented on GET destination-uris).
_LIST_PAGE_SIZE = 50
# Hard stop so a next_page_uri that never clears cannot page forever.
_LIST_MAX_ITEMS = 2000

# Inbound on Exotel needs App Bazaar work with no API behind it: a Flow whose
# Connect applet dials our trunk, and every DID's VoiceUrl pointed at that Flow.
# One key, not one per console action — nothing acts on a partially-done setup,
# so recording it would only invite a tenant to mark half and wonder why inbound
# is still dead.
CHECKLIST_CONSOLE_SETUP = "console_setup_confirmed"

# Exotel rejects anything outside ``[A-Za-z0-9_]`` in a trunk name, a credential
# friendly_name and — undocumented, and the reason this constant exists — a SIP
# digest password, all with 400 error 1002. Measured; the credentials page says
# only "use a strong secret" and its own friendly_name examples ("livekit-staging")
# would be rejected.
#
# `secrets.token_urlsafe` draws from a 64-character alphabet that includes `-`,
# so it produced a password Exotel refused roughly one provision in five — an
# intermittent failure landing after the trunk and destination URI were already
# created. Drawing from this alphabet instead makes it impossible rather than
# unlikely. 24 characters of it is ~143 bits, more than the 16-char token it
# replaces.
_PASSWORD_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_PASSWORD_LENGTH = 24


def _api_base(account: TelephonyAccount) -> str:
    """API host for this account, taken verbatim from its ``subdomain``.

    Exotel shows Account SID, region and subdomain together in the console, and
    the subdomain is the one that decides the API host — today
    ``api.in.exotel.com`` (Mumbai) or ``api.exotel.com`` (Singapore), but that
    list is Exotel's to change. Region is recorded alongside it for humans and
    is deliberately not consulted here: deriving the host from a guessed region
    is what made a Singapore account fail with a bare "Authorization failed".

    No default. An account connected without a subdomain has no correct host to
    fall back to, and picking one silently sends every call to the wrong region.
    """
    subdomain = str(account.account_info.get("subdomain") or "").strip().lower()
    if not subdomain:
        raise ProviderError(
            "Exotel account_info.subdomain is required — copy it from the Exotel "
            "console (e.g. api.exotel.com for Singapore, api.in.exotel.com for Mumbai)"
        )
    subdomain = subdomain.removeprefix("https://").removeprefix("http://").rstrip("/")
    return f"https://{subdomain}"


def _raise_on_partial_failure(method: str, path: str, data: dict[str, Any]) -> None:
    """Reject a 207 Multi-Status that reports failed entries.

    Exotel's batch endpoints (destination-uris, settings) answer "200 OK or 207
    Multi-Status (for partial success/failure)", so a 2xx alone does not mean the
    call did what we asked. Without this, a destination URI that failed to map is
    recorded as configured and the account goes ``ready`` with inbound dead.

    Documented shape: ``{metadata: {total, success, failed}, response: [{code,
    error_data, status, data}]}``.
    """
    metadata = data.get("metadata")
    failed = metadata.get("failed") if isinstance(metadata, dict) else None
    if not isinstance(failed, int) or failed < 1:
        return
    items = data.get("response")
    reasons = [
        f"code={item.get('code')} {item.get('error_data')}"
        for item in (items if isinstance(items, list) else [])
        if isinstance(item, dict) and item.get("status") != "success"
    ]
    total = metadata.get("total") if isinstance(metadata, dict) else None
    raise ProviderError(
        f"Exotel API {method} {path} partially failed: "
        f"{failed} of {total} entries rejected"
        + (f" ({'; '.join(reasons)})" if reasons else " (no per-entry detail returned)")
    )


def _body_failure_code(data: dict[str, Any]) -> int | None:
    """The status Exotel put in the body when it did not put it on the response.

    Exotel does not always answer a failure with a failing HTTP status. A
    duplicate trunk name comes back as ``200 OK`` carrying::

        {"http_code": 200,
         "response": {"code": 409, "status": "failure",
                      "error_data": {"code": 1008, "message": "Duplicate resource"},
                      "data": null}}

    Measured against a live account; no Exotel page documents it. Reading only
    the HTTP status turns that into a success with no ``trunk_sid``, which
    makes every branch keyed on 409 unreachable — including the adopt-an-
    orphaned-trunk recovery in ``ensure_account_sip``, whose whole reason to
    exist is this exact response.

    Returns None when the body reports no failure. Note the envelope also has a
    top-level ``http_code``, which is *not* consulted: on this response it says
    200 while the nested code says 409, so the nested one is the honest field.
    """
    body = data.get("response")
    if not isinstance(body, dict) or body.get("status") != "failure":
        return None
    code = body.get("code")
    # A failure Exotel refused to give a code is still a failure; 400 keeps the
    # caller on the generic path rather than letting it read as success.
    return code if isinstance(code, int) else 400


def _list_envelope_items(data: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Unwrap a paginated GET body into its objects, or None if it is not that shape.

    Documented on GET destination-uris and shared by the other list endpoints::

        {request_id, method, http_code,
         metadata: {page_size, first_page_uri, prev_page_uri, next_page_uri},
         response: [{code, error_data, status, data: {...}}]}

    An empty ``response`` returns ``[]``, which means "no rows". ``None`` means
    the body did not match, so a caller can never read a parse failure as an
    empty result.
    """
    entries = data.get("response")
    if not isinstance(entries, list):
        return None
    items: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("data"), dict):
            return None
        items.append(entry["data"])
    return items


def _list_has_next_page(data: dict[str, Any]) -> bool:
    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        return False
    return bool(str(metadata.get("next_page_uri") or "").strip())


def _normalize_sip_destination(value: str) -> str:
    """Canonical form for comparing a destination we sent against one Exotel echoes.

    We post the bare ``host:port;transport=…`` that ``LiveKitSipConfig`` holds,
    while Exotel's examples show the same value with a ``sip:`` scheme, so the
    two forms have to compare equal.
    """
    text = value.strip().lower()
    return text[4:] if text.startswith("sip:") else text


def exotel_inbound_console_ready(provider_state: dict[str, Any]) -> bool:
    """True once the tenant has confirmed the App Bazaar setup."""
    checklist = provider_state.get("checklist")
    if not isinstance(checklist, dict):
        return False
    return checklist.get(CHECKLIST_CONSOLE_SETUP) is True


def set_exotel_console_setup(provider_state: dict[str, Any], confirmed: bool) -> dict[str, Any]:
    """Record the tenant's confirmation that the App Bazaar setup is done."""
    state = dict(provider_state)
    checklist = (
        dict(state.get("checklist") or {}) if isinstance(state.get("checklist"), dict) else {}
    )
    checklist[CHECKLIST_CONSOLE_SETUP] = confirmed
    state["checklist"] = checklist
    return state


class ExotelAdapter:
    provider = "exotel"

    def _creds(self, account: TelephonyAccount, secrets: dict[str, str]) -> tuple[str, str, str]:
        account_sid = str(account.account_info.get("account_sid") or "").strip()
        if not account_sid:
            raise ProviderError("Exotel account_info.account_sid is required")
        api_key = resolve_secretish(account.credentials.get("api_key"), secrets)
        api_token = resolve_secretish(account.credentials.get("api_token"), secrets)
        if not api_key or not api_token:
            raise ProviderError("Exotel api_key and api_token secret refs are required")
        return account_sid, api_key, api_token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        account: TelephonyAccount,
        secrets: dict[str, str],
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        ignore_statuses: tuple[int, ...] = (),
    ) -> dict[str, Any]:
        account_sid, api_key, api_token = self._creds(account, secrets)
        base = _api_base(account)
        url = f"{base}/v2/accounts/{account_sid}{path}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(
                method,
                url,
                auth=(api_key, api_token),
                json=json_body,
                params=params,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
        if resp.status_code in ignore_statuses:
            return {}
        if resp.status_code >= 400:
            # Full body and headers to the log; the ProviderError below is
            # truncated because it ends up on the dashboard. Exotel's body
            # carries request_id and a numeric code, both worth having.
            logger.error(
                "Exotel API %s %s failed (%s)\nheaders: %s\nbody: %s",
                method,
                url,
                resp.status_code,
                dict(resp.headers),
                resp.text,
            )
            detail = resp.text[:500]
            if resp.status_code == 401:
                # Exotel answers a right-key-wrong-region request with a bare
                # "Authorization failed", identical to a bad key. Name the other
                # cause: credentials are per-region, so a Singapore account hit
                # on api.in.exotel.com fails exactly like a typo'd token.
                detail = (
                    f"{detail} — check api_key/api_token AND that subdomain "
                    f"({base.removeprefix('https://')}) matches the account's region"
                )
            raise ProviderError(
                f"Exotel API {method} {path} failed ({resp.status_code}): {detail}",
                status_code=resp.status_code,
            )
        if not resp.content:
            return {}
        data = resp.json()
        if not isinstance(data, dict):
            raise ProviderError("Exotel API returned non-object JSON")
        # Checked against the body's own status, not the HTTP one, and honouring
        # ignore_statuses the same way — a DELETE whose target is already gone
        # should stay a no-op whichever place Exotel puts the 404.
        body_code = _body_failure_code(data)
        if body_code is not None:
            if body_code in ignore_statuses:
                return {}
            logger.error(
                "Exotel API %s %s reported failure in a %s body\nbody: %s",
                method,
                url,
                resp.status_code,
                resp.text,
            )
            raise ProviderError(
                f"Exotel API {method} {path} failed ({body_code}): {resp.text[:500]}",
                status_code=body_code,
            )
        _raise_on_partial_failure(method, path, data)
        return data

    async def validate_credentials(
        self, account: TelephonyAccount, secrets: dict[str, str]
    ) -> None:
        # List trunks is a cheap authenticated call.
        await self._request("GET", "/trunks", account=account, secrets=secrets)

    async def list_remote_numbers(
        self, account: TelephonyAccount, secrets: dict[str, str]
    ) -> list[RemoteNumberInfo]:
        account_sid, api_key, api_token = self._creds(account, secrets)
        base = _api_base(account)
        # ExoPhone inventory lives under a separate API family.
        url = f"{base}/v2_beta/Accounts/{account_sid}/IncomingPhoneNumbers"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                url, auth=(api_key, api_token), headers={"Accept": "application/json"}
            )
        if resp.status_code >= 400:
            logger.error(
                "Exotel API GET %s failed (%s)\nheaders: %s\nbody: %s",
                url,
                resp.status_code,
                dict(resp.headers),
                resp.text,
            )
            # 404 / empty inventory is different from auth/API failure — fail loud.
            detail = resp.text[:400]
            if resp.status_code in (401, 403):
                raise ProviderError(
                    f"Exotel list numbers failed ({resp.status_code}): check api_key/api_token "
                    "and that subdomain matches the account's region",
                    status_code=resp.status_code,
                )
            if resp.status_code == 404:
                # An account with no ExoPhones still returns 200 with an empty
                # list, so a 404 means the path or account_sid is wrong.
                raise ProviderError(
                    f"Exotel list numbers returned 404 for {url}; "
                    "confirm account_sid and subdomain",
                    status_code=404,
                )
            raise ProviderError(
                f"Exotel list numbers failed ({resp.status_code}): {detail}",
                status_code=resp.status_code,
            )
        data = resp.json()
        # Documented shape: {page, page_size, incoming_phone_numbers: [{sid,
        # phone_number, friendly_name, capabilities, country, region}]}. Indexed
        # rather than guessed at, so a shape change raises here instead of
        # importing zero numbers and looking like an empty account.
        if not isinstance(data, dict) or "incoming_phone_numbers" not in data:
            raise ProviderError(
                "Exotel list numbers response has no 'incoming_phone_numbers' key "
                f"(got {sorted(data) if isinstance(data, dict) else type(data).__name__}); "
                "the ExoPhones API shape may have changed"
            )
        items = data["incoming_phone_numbers"]
        if not isinstance(items, list):
            raise ProviderError(
                f"Exotel incoming_phone_numbers is {type(items).__name__}, expected a list"
            )
        # The response carries page/page_size but Exotel documents no request
        # parameter for asking for page 1, so we can only ever read the first
        # page. Landing exactly on the boundary means there are probably more.
        page_size = data.get("page_size")
        if isinstance(page_size, int) and len(items) >= page_size:
            logger.warning(
                "Exotel returned a full page of %d numbers for account %s; "
                "further pages cannot be fetched (no documented pagination parameter), "
                "so the import list may be incomplete",
                page_size,
                account_sid,
            )
        out: list[RemoteNumberInfo] = []
        for item in items:
            raw = str(item.get("phone_number") or "").strip()
            if not raw:
                continue
            try:
                e164 = normalize_e164(raw)
            except ValueError:
                # One malformed row should not fail the whole import.
                logger.warning("skipping unparseable Exotel number %r", raw)
                continue
            out.append(
                RemoteNumberInfo(
                    e164=e164,
                    provider_number_id=str(item.get("sid") or "") or None,
                    label=str(item.get("friendly_name") or "").strip() or None,
                )
            )
        return out

    async def _find_trunk_sid_by_name(
        self,
        account: TelephonyAccount,
        secrets: dict[str, str],
        *,
        trunk_name: str,
    ) -> str | None:
        """Resolve a trunk_sid from its name by walking GET /trunks.

        Returns None only when Exotel's list genuinely has no trunk by that
        name; an unreadable page raises instead, so "not found" is never a
        parse failure in disguise.
        """
        offset = 0
        while offset < _LIST_MAX_ITEMS:
            data = await self._request(
                "GET",
                "/trunks",
                account=account,
                secrets=secrets,
                params={"page_size": _LIST_PAGE_SIZE, "offset": offset},
            )
            items = _list_envelope_items(data)
            if items is None:
                raise ProviderError(
                    "Exotel GET /trunks did not return the documented list envelope "
                    "({metadata, response: [{code, status, data}]}), so the trunk list "
                    "cannot be read"
                )
            if not items:
                return None
            for item in items:
                if str(item.get("trunk_name") or "").strip() == trunk_name:
                    found = str(item.get("trunk_sid") or "").strip()
                    if found:
                        return found
            if not _list_has_next_page(data):
                return None
            offset += len(items)
        logger.warning(
            "Exotel trunk list hit the %s-item cap without finding %s",
            _LIST_MAX_ITEMS,
            trunk_name,
        )
        return None

    async def _destination_uri_present(
        self,
        account: TelephonyAccount,
        secrets: dict[str, str],
        *,
        trunk_sid: str,
        destination: str,
    ) -> bool | None:
        """Is ``destination`` already mapped on this trunk? None when unreadable.

        None rather than False on a page we cannot parse: the caller should then
        post the destination anyway, because a duplicate entry is a smaller
        problem than a trunk with no route back to us.
        """
        wanted = _normalize_sip_destination(destination)
        offset = 0
        while offset < _LIST_MAX_ITEMS:
            data = await self._request(
                "GET",
                f"/trunks/{trunk_sid}/destination-uris",
                account=account,
                secrets=secrets,
                params={"page_size": _LIST_PAGE_SIZE, "offset": offset},
            )
            items = _list_envelope_items(data)
            if items is None:
                return None
            if not items:
                return False
            for item in items:
                if _normalize_sip_destination(str(item.get("destination") or "")) == wanted:
                    return True
            if not _list_has_next_page(data):
                return False
            offset += len(items)
        logger.warning("Exotel destination-uri list hit the %s-item cap", _LIST_MAX_ITEMS)
        return None

    async def ensure_account_sip(
        self,
        account: TelephonyAccount,
        secrets: dict[str, str],
        *,
        platform_sip_uri: str,
    ) -> tuple[dict[str, Any], str | None]:
        account_sid = str(account.account_info.get("account_sid") or "").strip()
        state = dict(account.provider_state)
        generated_sip_password: str | None = None

        trunk_sid = str(state.get("trunk_sid") or "").strip()
        adopted_existing_trunk = False
        if not trunk_sid:
            # trunk_name max 16 chars alphanumeric + underscore
            trunk_name = f"tq_{account.id.hex[:6]}"
            domain = f"{account_sid}.pstn.exotel.com"
            try:
                created = await self._request(
                    "POST",
                    "/trunks",
                    account=account,
                    secrets=secrets,
                    json_body={
                        "trunk_name": trunk_name,
                        # "ANY-ANY", not "any any": the hyphen and the case are
                        # both load-bearing. A space is rejected with 400 error
                        # 1002 "Nso code is invalid" — verified live, and it is
                        # the spelling Exotel's Create Trunk page documents.
                        "nso_code": "ANY-ANY",
                        "domain_name": domain,
                    },
                )
            except ProviderError as exc:
                # 409 is documented as error 1008, duplicate resource. The name is
                # derived from the account id and so never changes, which means
                # Exotel still holds a trunk we created while our copy of the sid
                # is gone — a provision that died between this POST and the state
                # write. Re-create is impossible and delete needs the sid, so the
                # only way out is to look the trunk up by name and adopt it.
                if exc.status_code != 409:
                    raise
                trunk_sid = await self._find_trunk_sid_by_name(
                    account, secrets, trunk_name=trunk_name
                )
                if not trunk_sid:
                    raise ProviderError(
                        f"Exotel rejected trunk {trunk_name} as a duplicate but does not list a "
                        "trunk by that name, so its trunk_sid cannot be recovered and deleting "
                        f"it requires that sid: delete the {trunk_name} trunk in the Exotel "
                        "console, then provision again."
                    ) from exc
                adopted_existing_trunk = True
                logger.info("adopted existing Exotel trunk %s (%s)", trunk_sid, trunk_name)
            else:
                # Response shapes vary: response.response.data / trunk_sid top-level
                trunk_sid = _dig_str(created, "trunk_sid")
                if not trunk_sid:
                    raise ProviderError(
                        "Exotel create trunk did not return trunk_sid. "
                        "Confirm SIP trunking is enabled on this account (often support-gated)."
                    )
            state["trunk_sid"] = trunk_sid
            state["domain_name"] = domain
            state["trunk_name"] = trunk_name

        # Destination URI → our livekit-sip
        dest = str(state.get("destination_uri") or "").strip()
        if dest != platform_sip_uri:
            already_mapped = False
            if adopted_existing_trunk:
                # An adopted trunk carries whatever the lost provision managed to
                # configure. This POST appends rather than replaces, so without
                # the check a recovery would leave two identical destinations.
                present = await self._destination_uri_present(
                    account, secrets, trunk_sid=trunk_sid, destination=platform_sip_uri
                )
                if present is None:
                    logger.warning(
                        "could not read destination URIs on adopted trunk %s; posting %s "
                        "anyway, which may duplicate an existing entry",
                        trunk_sid,
                        platform_sip_uri,
                    )
                already_mapped = present is True
            if already_mapped:
                logger.info(
                    "adopted trunk %s already routes to %s; leaving it alone",
                    trunk_sid,
                    platform_sip_uri,
                )
            else:
                await self._request(
                    "POST",
                    f"/trunks/{trunk_sid}/destination-uris",
                    account=account,
                    secrets=secrets,
                    json_body={"destinations": [{"destination": platform_sip_uri}]},
                )
            state["destination_uri"] = platform_sip_uri

        # No IP ACL — digest (below) is the only outbound auth. Exotel's LiveKit
        # guide calls the trunk ACL optional ("use whitelisted ips only when
        # livekit ... provides a fixed static egress ip ... otherwise rely on
        # digest") and reports outbound as validated on digest alone. Their
        # Dynamic SIP Trunking FAQ contradicts this and calls the ACL mandatory
        # for outbound; the LiveKit guide wins because it covers this exact
        # integration. If outbound ever 403s, check digest, edge host/port and
        # E.164 first — Exotel's own troubleshooting order — before suspecting
        # the missing ACL.
        #
        # The ACL would also defeat the hostname indirection: accounts are pinned
        # to a name (LiveKitSipConfig), so replacing the SIP edge is an A-record
        # change with no provider-side work, whereas an ACL stores an IP and
        # would silently break outbound until every trunk was re-pushed.

        # Digest credentials for outbound
        if not str(state.get("sip_username") or "").strip():
            # token_hex is already inside the allowed alphabet; the password is
            # not, so it is drawn character by character instead.
            username = "tq" + pysecrets.token_hex(4)
            password = "".join(
                pysecrets.choice(_PASSWORD_ALPHABET) for _ in range(_PASSWORD_LENGTH)
            )
            created = await self._request(
                "POST",
                f"/trunks/{trunk_sid}/credentials",
                account=account,
                secrets=secrets,
                json_body={
                    "user_name": username,
                    "password": password,
                    # Underscore, not a hyphen: Exotel rejects anything outside
                    # alphanumerics and `_` here with 400 error 1002 "Allowed
                    # characters for FriendlyName is alphanumberic and _" (sic).
                    "friendly_name": f"talqing_{account.id.hex[:6]}",
                },
            )
            state["sip_username"] = username
            state["credential_id"] = _dig_str(created, "id") or _dig_str(created, "credential_id")
            generated_sip_password = password

        # Edge host: account_info overrides (set at connect) beat defaults.
        info = account.account_info
        edge_host = (
            str(info.get("outbound_edge_host") or "").strip()
            or str(state.get("outbound_edge_host") or "").strip()
        )
        if not edge_host:
            raise ProviderError(
                "Exotel outbound_edge_host is not set. Exotel assigns this per account "
                "and it is region-specific, so there is no safe default — get it from "
                "Exotel and set it on the account."
            )
        raw_port = info.get("outbound_edge_port")
        if raw_port is None or str(raw_port).strip() == "":
            raw_port = state.get("outbound_edge_port", _DEFAULT_OUTBOUND_EDGE_PORT)
        try:
            edge_port = int(raw_port)
        except (TypeError, ValueError) as exc:
            raise ProviderError(
                f"invalid outbound_edge_port {raw_port!r}; use an integer port"
            ) from exc
        transport = (
            str(info.get("outbound_transport") or "").strip().lower()
            or str(state.get("outbound_transport") or "").strip().lower()
            or _DEFAULT_OUTBOUND_TRANSPORT
        )
        if transport not in ("tcp", "tls", "udp"):
            raise ProviderError(f"invalid outbound_transport {transport!r}; use tcp, tls, or udp")
        state["outbound_edge_host"] = edge_host
        state["outbound_edge_port"] = edge_port
        state["outbound_transport"] = transport

        # Re-provisioning must not silently un-confirm console work the tenant
        # has already done — the trunk_sid they wired up has not changed.
        existing_checklist = (
            state.get("checklist") if isinstance(state.get("checklist"), dict) else {}
        )
        state["checklist"] = {
            CHECKLIST_CONSOLE_SETUP: existing_checklist.get(CHECKLIST_CONSOLE_SETUP) is True,
        }
        return state, generated_sip_password

    def _console_base(self, account: TelephonyAccount) -> str | None:
        """Root of this account's Exotel console, or None if we cannot be sure.

        Exotel's console mirrors its API host one label at a time —
        ``api.in.exotel.com`` ⇄ ``my.in.exotel.com``, ``api.exotel.com`` ⇄
        ``my.exotel.com`` — and every page is scoped by account SID underneath.
        Both hosts are confirmed to exist, so this swap is not the region
        guessing that ``_api_base`` refuses to do: the input is the subdomain
        the tenant copied out of that very console, and the worst case is a
        404 in a new tab rather than a call sent to the wrong country.

        Returns None on any subdomain that does not start with ``api.`` — a
        guessed console link is worse than a step with no link, because a tenant
        who lands on someone else's 404 has no way to tell whether they or we
        got it wrong.
        """
        subdomain = str(account.account_info.get("subdomain") or "").strip().lower()
        account_sid = str(account.account_info.get("account_sid") or "").strip()
        if not account_sid or not subdomain.startswith("api."):
            return None
        return f"https://my.{subdomain.removeprefix('api.')}/{account_sid}"

    def setup_steps(self, account: TelephonyAccount) -> list[TelephonySetupStep]:
        state = account.provider_state
        trunk_sid = str(state.get("trunk_sid") or "").strip()
        console = self._console_base(account)
        console_url = f"{console}/apps" if console else None
        numbers_url = f"{console}/numbers" if console else None
        if not trunk_sid:
            return [
                TelephonySetupStep(
                    key="trunk",
                    title="Connect to Exotel",
                    detail="Talqing does this for you — run setup on this carrier.",
                    done=False,
                )
            ]
        return [
            TelephonySetupStep(
                key="trunk",
                title="Connected to Exotel",
                done=True,
            ),
            TelephonySetupStep(
                key=CHECKLIST_CONSOLE_SETUP,
                title="Point your numbers at Talqing, in Exotel",
                # Leading with *why* because the shape of the work is surprising:
                # on every other carrier we support a number points at a trunk,
                # so a tenant looking for that setting in Exotel will not find it
                # and will assume they are on the wrong page.
                detail=(
                    "Exotel can only hand an incoming call to a Flow, never straight to a "
                    "trunk — so your numbers need a small Flow that passes the call to "
                    "Talqing. It takes about two minutes, once per account."
                ),
                instructions=[
                    TelephonySetupInstruction(
                        text=(
                            "In Exotel's App Bazaar, under Custom Apps, click Create. "
                            "Name it something like “Talqing inbound”."
                        ),
                        url=console_url,
                        url_label="Open App Bazaar",
                    ),
                    # Naming both the option to keep and the field to leave alone,
                    # because the Connect applet opens with a big required-looking
                    # "Primary URL *" directly under the question — it belongs to
                    # the dynamic-URL mode nobody here wants, and it is the first
                    # box a reader's eye lands on.
                    TelephonySetupInstruction(
                        text=(
                            "Drag the Connect applet from Voice Applets onto “Call Start”. "
                            "Leave “Configure using flow builder here” selected, and leave "
                            "both Primary URL boxes empty — those are for calling a webhook "
                            "of your own, not for reaching Talqing."
                        ),
                    ),
                    TelephonySetupInstruction(
                        text=(
                            "Under Dial Whom, pick “Dial phone number(s)” and paste this "
                            "into that box, then save the Flow."
                        ),
                        copy_value=f"sip:{trunk_sid}",
                        copy_hint=(
                            "Your Talqing SIP trunk on Exotel. The box asks for a phone "
                            "number and the sip: prefix is what overrides that — it tells "
                            "Exotel to hand the call to a trunk instead of dialling anyone."
                        ),
                    ),
                    TelephonySetupInstruction(
                        text=(
                            "In ExoPhones, click Assign ExoPhones to Flow, tick every number "
                            "Talqing should answer and the Flow you just made, then click "
                            "Attach Flow. A number still showing “Install an App” is not "
                            "connected yet."
                        ),
                        url=numbers_url,
                        url_label="Open ExoPhones",
                    ),
                ],
                doc_url=(
                    "https://docs.exotel.com/dynamic-sip-trunking/"
                    "connect-applet-setup-for-inbound-sip-trunking"
                ),
                doc_label="Exotel's guide to the Connect applet",
                done=exotel_inbound_console_ready(state),
                user_confirmable=True,
            ),
        ]

    def outbound_sip_configured(self, provider_state: dict[str, Any]) -> bool:
        return bool(str(provider_state.get("sip_username") or "").strip())

    async def bind_number(
        self,
        account: TelephonyAccount,
        secrets: dict[str, str],
        *,
        e164: str,
        number_provider_state: dict[str, Any],
    ) -> dict[str, Any]:
        trunk_sid = str(account.provider_state.get("trunk_sid") or "").strip()
        if not trunk_sid:
            raise ProviderError("Exotel account has no trunk_sid; provision the account first")
        created = await self._request(
            "POST",
            f"/trunks/{trunk_sid}/phone-numbers",
            account=account,
            secrets=secrets,
            json_body={"phone_number": e164, "mode": "pstn"},
        )
        mapping_id = _dig_str(created, "id") or created.get("id")
        # Trunk-level CLI alias is account-scoped — set once so multi-DID binds
        # do not overwrite each other. Per-call From is still sip_number on dial.
        existing_alias = str(account.provider_state.get("trunk_external_alias") or "").strip()
        if not existing_alias:
            try:
                await self._request(
                    "POST",
                    f"/trunks/{trunk_sid}/settings",
                    account=account,
                    secrets=secrets,
                    json_body={"settings": [{"name": "trunk_external_alias", "value": e164}]},
                )
                # Persist on account state via return path: service merges number
                # state only; stash on number state for visibility.
            except ProviderError as exc:
                logger.warning("Exotel trunk_external_alias failed: %s", exc.message)

        state = dict(number_provider_state)
        state["phone_number_mapping_id"] = mapping_id
        state["mode"] = "pstn"
        state["checklist_did_on_flow"] = False
        # Inbound needs App Bazaar Flow mapping — not automated.
        state["inbound_console_pending"] = not exotel_inbound_console_ready(account.provider_state)
        if not existing_alias:
            state["set_trunk_external_alias"] = e164
        return state

    async def unbind_number(
        self,
        account: TelephonyAccount,
        secrets: dict[str, str],
        *,
        e164: str,
        number_provider_state: dict[str, Any],
    ) -> dict[str, Any]:
        del e164
        state = dict(number_provider_state)
        trunk_sid = str(account.provider_state.get("trunk_sid") or "").strip()
        mapping_id = str(state.get("phone_number_mapping_id") or "").strip()
        if not trunk_sid or not mapping_id:
            # Never mapped, or mapped before we recorded the id. The API keys the
            # mapping by its numeric id, not by the number, so there is nothing
            # we can address here.
            return state
        # Postman-collection evidence only (see module docstring) — the curl,
        # python and go samples skip this call and the reference page omits it.
        # 404 is ignored because a mapping that is already gone is the outcome we
        # want, but note it would also hide the endpoint not existing at all; a
        # 405 raises, which is the signal we would actually get in that case.
        await self._request(
            "DELETE",
            f"/trunks/{trunk_sid}/phone-numbers",
            account=account,
            secrets=secrets,
            params={"id": mapping_id},
            ignore_statuses=(404,),
        )
        state.pop("phone_number_mapping_id", None)
        logger.info("deleted Exotel phone-number mapping %s on trunk %s", mapping_id, trunk_sid)
        return state

    async def teardown_account(self, account: TelephonyAccount, secrets: dict[str, str]) -> None:
        state = account.provider_state
        trunk_sid = str(state.get("trunk_sid") or "").strip()
        if not trunk_sid:
            return
        # Deleting the trunk cascades to phone numbers, ACLs and destination
        # URIs, but credentials are not in that list — revoke ours explicitly,
        # and first, so a failure here cannot strand a working outbound login
        # behind an already-deleted trunk.
        credential_id = str(state.get("credential_id") or "").strip()
        if credential_id:
            await self._request(
                "DELETE",
                f"/trunks/{trunk_sid}/credentials",
                account=account,
                secrets=secrets,
                params={"id": credential_id},
                ignore_statuses=(404,),
            )
            logger.info("deleted Exotel credential %s on trunk %s", credential_id, trunk_sid)
        await self._request(
            "DELETE",
            "/trunks",
            account=account,
            secrets=secrets,
            params={"trunk_sid": trunk_sid},
            ignore_statuses=(404,),
        )
        logger.info("deleted Exotel trunk %s", trunk_sid)

    def outbound_inline_config(
        self,
        account: TelephonyAccount,
        secrets: dict[str, str],
        *,
        from_e164: str,
    ) -> OutboundInlineConfig:
        del from_e164
        host = str(account.provider_state.get("outbound_edge_host") or "").strip()
        port = account.provider_state.get("outbound_edge_port")
        transport = str(account.provider_state.get("outbound_transport") or "tls").strip()
        username = str(account.provider_state.get("sip_username") or "").strip()
        password = resolve_secretish(account.credentials.get("sip_outbound_password"), secrets)
        if not host or not username or not password:
            raise ProviderError(
                "Exotel outbound SIP is not provisioned "
                "(missing edge host, sip_username, or sip_outbound_password). "
                "Re-provision the account and set outbound_edge_host to the edge "
                "Exotel assigned it."
            )
        hostname = f"{host}:{port}" if port else host
        return OutboundInlineConfig(
            hostname=hostname,
            auth_username=username,
            auth_password=password,
            transport=transport if transport in ("tcp", "tls", "udp") else "tls",
        )

    def refer_uri(self, account: TelephonyAccount, *, e164: str, direction: str) -> str:
        del account, e164, direction
        # `supports_refer` is False for Exotel, so a transfer here is routed to
        # the bridge. Reaching this is a routing bug in the worker, not a
        # carrier limitation — say so rather than guessing at a URI.
        raise ProviderError(
            "Exotel does not support SIP REFER transfers; transfers on this "
            "carrier are bridged into the caller's room instead"
        )


def _dig_str(data: dict[str, Any], key: str) -> str:
    if key in data and data[key] is not None:
        return str(data[key]).strip()
    # nested response.response / response.data patterns
    for nest in ("response", "data", "object", "Result"):
        child = data.get(nest)
        if isinstance(child, dict):
            found = _dig_str(child, key)
            if found:
                return found
        if isinstance(child, list) and child and isinstance(child[0], dict):
            found = _dig_str(child[0], key)
            if found:
                return found
    return ""
