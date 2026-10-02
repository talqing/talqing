"""The credentials this platform issues, and the state it round-trips through
a third party.

Two key kinds, and which one a thing uses is a question about *where it is read*
rather than about how sensitive it is.

**Sessions and personal access tokens are Ed25519 (EdDSA).** The control plane
mints them and every region verifies them, so the key that verifies has to be on
every region — and with HS256 that is the same key that mints, which makes every
region able to forge a credential for every other one. Region isolation would be
illusory. The private half lives only on the control droplet
(``security.session_signing_key``, refused in a region's config); the public half
is everywhere. Verification stays local and offline, with no round trip — which
matters more than it looks, because there is no cache in front of the control
plane and signature checking must never become a network call.

**A chat token is HS256 too, and a region's own.** ``POST /v1/chats/{id}/token``
mints one for a browser and the same region reads it back, so it is signed with
``security.chat_token_secret`` — a region holds no key that could sign with the
credential algorithm, by design.

**OAuth state stays HS256.** ``create_google_login_state`` on control and
``create_oauth_state`` on a region are each minted and read back by the same
process on the same host, never cross a host, and each node generates its own
``security.oauth_state_key``. A key pair would buy nothing.

Sessions are stateless: the JWT is the whole credential, with no server-side
store, so there is no instant revocation before it expires. A PAT is different —
its ``jti`` is a control-plane row id and that row IS the revocation check.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

import jwt

from settings import get_settings

SESSION_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days
OAUTH_STATE_TTL_SECONDS = 10 * 60
CHAT_TOKEN_TTL_SECONDS = 60 * 60
# Ed25519. PyJWT spells it `EdDSA` and takes PEM strings directly.
_CREDENTIAL_ALGO = "EdDSA"
# HS256, for state one host mints and reads back itself.
_STATE_ALGO = "HS256"


def _signing_key() -> str:
    key = get_settings().security.session_signing_key
    if not key:
        # Reachable only by calling a mint from a region, which is a bug in the
        # route split rather than a configuration mistake — say which.
        raise RuntimeError(
            "this node has no session signing key: minting a session or a personal "
            "access token is the control plane's job (api.control), not a region's"
        )
    return key


def _verify_key() -> str:
    return get_settings().security.session_verify_key


def _state_key() -> str:
    return get_settings().security.oauth_state_key


def create_session(user_id: str, tenant_id: str) -> str:
    now = datetime.now(UTC)
    payload = {
        "user_id": user_id,
        "tenant_id": tenant_id,
        "iat": now,
        "exp": now + timedelta(seconds=SESSION_TTL_SECONDS),
    }
    return jwt.encode(payload, _signing_key(), algorithm=_CREDENTIAL_ALGO)


def read_session(token: str) -> dict[str, str] | None:
    try:
        d = jwt.decode(token, _verify_key(), algorithms=[_CREDENTIAL_ALGO])
    except jwt.PyJWTError:
        return None
    if d.get("typ") == "pat":
        # a PAT in the session cookie would bypass the revocation lookup
        return None
    return {"user_id": d["user_id"], "tenant_id": d["tenant_id"]}


def create_pat(user_id: str, tenant_id: str, token_id: str) -> str:
    """Personal access token: a no-expiry JWT whose jti is the DB row id.
    Revocation is deleting that row (checked on every request) — the signature
    alone is never enough.

    The payload is fixed, with no `iat`, so a row has exactly one token string
    and calling this again re-derives it rather than issuing a second valid
    credential. That is what lets the dashboard show a user their own MCP token
    without the plaintext ever being stored (see routes.tokens.mcp_token). An
    `iat` would buy nothing here: the token does not expire, and nothing reads
    it.

    One token reaches every region. It is control-plane and names an
    organization, not a region, so an integrator holds one token and changes the
    base URL — there is no per-region token and none is wanted.
    """
    payload = {
        "typ": "pat",
        "user_id": user_id,
        "tenant_id": tenant_id,
        "jti": token_id,
    }
    return jwt.encode(payload, _signing_key(), algorithm=_CREDENTIAL_ALGO)


def read_pat(token: str) -> dict[str, str] | None:
    try:
        d = jwt.decode(token, _verify_key(), algorithms=[_CREDENTIAL_ALGO])
    except jwt.PyJWTError:
        return None
    if d.get("typ") != "pat" or not d.get("jti"):
        return None
    return {"user_id": d["user_id"], "tenant_id": d["tenant_id"], "jti": d["jti"]}


def create_chat_token(tenant_id: str, chat_id: str) -> tuple[str, datetime]:
    """A browser's credential for ONE chat, and when it stops working.

    Pinned to the chat, and through it to the one contact the tenant's server
    chose: a browser never names a contact, so it can never read another
    person's history. There is no refresh grant — the tenant's server mints
    another.
    """
    expires_at = datetime.now(UTC) + timedelta(seconds=CHAT_TOKEN_TTL_SECONDS)
    payload = {"typ": "chat", "tid": tenant_id, "cid": chat_id, "exp": expires_at}
    token = jwt.encode(payload, get_settings().security.chat_token_secret, algorithm=_STATE_ALGO)
    return token, expires_at


def read_chat_token(token: str) -> dict[str, str] | None:
    """The tenant and chat a chat token names, or None when it is not a live one."""
    try:
        d = jwt.decode(
            token,
            get_settings().security.chat_token_secret,
            algorithms=[_STATE_ALGO],
            options={"require": ["exp"]},
        )
    except jwt.PyJWTError:
        return None
    if d.get("typ") != "chat" or not d.get("tid") or not d.get("cid"):
        return None
    return {"tenant_id": d["tid"], "chat_id": d["cid"]}


def create_google_login_state(*, country: str | None = None) -> str:
    """Short-lived CSRF state for the Google login OAuth redirect.

    It carries the visitor's country because that is the one thing that has to
    survive the round trip to Google: the dashboard reads it off Cloudflare's
    edge before anyone is signed in, and the callback maps it through control's
    region list to pick the new organization's home region. Signed, so it cannot
    be swapped between the redirect and the callback — though it is a
    client-supplied value either way, which is acceptable only because all it
    selects is where a first workspace is created (see the route).
    """
    now = datetime.now(UTC)
    payload = {
        "typ": "google_login_state",
        "nonce": secrets.token_urlsafe(24),
        "country": country,
        "iat": now,
        "exp": now + timedelta(seconds=OAUTH_STATE_TTL_SECONDS),
    }
    return jwt.encode(payload, _state_key(), algorithm=_STATE_ALGO)


def read_google_login_state(token: str) -> dict[str, str | None] | None:
    """The state's payload, or ``None`` when it is not one of ours.

    ``None`` is the CSRF failure; a dict with ``country: None`` is a perfectly
    normal login from a visitor whose country we could not read.
    """
    try:
        d = jwt.decode(token, _state_key(), algorithms=[_STATE_ALGO])
    except jwt.PyJWTError:
        return None
    if d.get("typ") != "google_login_state" or not d.get("nonce"):
        return None
    country = d.get("country")
    return {"country": country if isinstance(country, str) else None}


def create_oauth_state(
    *,
    user_id: str,
    tenant_id: str,
    provider: str,
    integration_id: str | None = None,
    extra: dict[str, str] | None = None,
) -> str:
    now = datetime.now(UTC)
    payload = {
        "typ": "oauth_state",
        "user_id": user_id,
        "tenant_id": tenant_id,
        "provider": provider,
        "integration_id": integration_id,
        "nonce": secrets.token_urlsafe(24),
        "iat": now,
        "exp": now + timedelta(seconds=OAUTH_STATE_TTL_SECONDS),
    }
    if extra:
        reserved = set(payload)
        overlap = reserved.intersection(extra)
        if overlap:
            raise ValueError(
                f"OAuth state extra fields use reserved keys: {', '.join(sorted(overlap))}"
            )
        payload.update(extra)
    return jwt.encode(payload, _state_key(), algorithm=_STATE_ALGO)


def read_oauth_state(token: str, *, provider: str) -> dict[str, str | None] | None:
    try:
        d = jwt.decode(token, _state_key(), algorithms=[_STATE_ALGO])
    except jwt.PyJWTError:
        return None
    if d.get("typ") != "oauth_state" or d.get("provider") != provider:
        return None
    out: dict[str, str | None] = {
        "user_id": d["user_id"],
        "tenant_id": d["tenant_id"],
        "integration_id": d.get("integration_id"),
    }
    reserved = {"typ", "provider", "nonce", "iat", "exp"} | set(out)
    for key, value in d.items():
        if key in reserved:
            continue
        out[key] = value if isinstance(value, str) else None
    return out
