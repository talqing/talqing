"""Process-wide settings: nested Pydantic models loaded from YAML.

Source of truth: ``configs/{ENV}.config.yaml`` where ``ENV`` names both the
environment and the node — ``local.control``, ``dev.in``, ``prod.us`` — and comes
from the process environment or ``backend/.env``.

**One value selects the file, and the filename says which node it is for.** The
head is the environment (``local``, ``dev``, ``prod``); the tail is either the
literal ``control`` or a region slug. That is what keeps a mis-``scp``'d config
from being catastrophic: ``prod.us.config.yaml`` on the India droplet is not
loaded and wrong, it is *not found*, and the process refuses to start naming the
file it wanted.

Two fields are derived from the tail rather than configured beside it, so they
cannot drift from the file that is actually loaded: :attr:`Settings.role`
(``control`` or ``region``) and :attr:`Settings.region` (the slug, unset on
control).

Missing required keys fail fast at startup (pydantic ValidationError), and
:meth:`Settings._role_carries_its_own_keys` is what makes that true per role —
the two nodes read the same model but not the same half of it.
"""

from __future__ import annotations

import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, Field, field_validator, model_validator

EnvName = Literal["local", "dev", "prod"]
NodeRole = Literal["control", "region"]
_VALID_ENVS = frozenset({"local", "dev", "prod"})
# The tail of ENV when this process is the control plane. Anything else is a
# region slug, which is a product identifier and never a datacenter: `in`, `us`,
# `eu` — never `blr1`, because moving India to a future Mumbai datacenter must
# not change a hostname a carrier has been given.
CONTROL_NODE = "control"
_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_CONFIGS_DIR = _BACKEND_ROOT / "configs"


# ── nested config models ─────────────────────────────────────────────────────


def _postgres_dsn(value: str, key: str) -> str:
    """Validate a DSN. Both planes hold one, so both are checked the same way.

    A blank or scheme-less value reaches asyncpg as a connect error on the first
    query rather than at boot, which for the control plane means the API passes
    its health check and then fails the first request that touches a tenant.
    """
    dsn = value.strip()
    if not dsn.startswith("postgresql://"):
        raise ValueError(f"{key} must be a postgresql:// DSN")
    return dsn


def _load_ed25519_private(pem: str) -> Ed25519PrivateKey:
    """Parse an unencrypted PKCS#8 PEM, naming the key that is wrong.

    Generate one with `cryptography`, NOT openssl: macOS ships LibreSSL, whose
    `genpkey` has no ed25519 at all. `backend/configs/example.control.config.yaml`
    carries the one-liner that writes both halves.
    """
    try:
        key = serialization.load_pem_private_key(pem.strip().encode(), password=None)
    except Exception as exc:
        raise ValueError(f"security.session_signing_key is not a readable PEM private key: {exc}")
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError(
            "security.session_signing_key must be an Ed25519 key — see "
            "configs/example.control.config.yaml for the one-liner that writes both halves"
        )
    return key


def _load_ed25519_public(pem: str) -> Ed25519PublicKey:
    try:
        key = serialization.load_pem_public_key(pem.strip().encode())
    except Exception as exc:
        raise ValueError(f"security.session_verify_key is not a readable PEM public key: {exc}")
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("security.session_verify_key must be an Ed25519 public key")
    return key


def _public_pem(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _internal_token(value: str, key: str) -> str:
    """One region's shared bearer for the region <-> control link.

    **One secret per region, held by exactly two nodes and used in both
    directions.** A region sends it as ``control.internal_token`` and control
    matches it against ``regions[<slug>].internal_token``; control sends the same
    value back to push credit and the region matches it against
    ``control.internal_token``. So those two fields MUST hold the same string,
    and a deployment where they differ 401s every authenticated request in that
    region.

    That is not a weakening: each region has its own secret, so a compromised
    region still cannot speak for another one — which is the isolation property
    that matters. A second secret would be held by the same two processes and
    guard nothing more.

    Generate with ``openssl rand -hex 32``. Short values are refused rather than
    warned about: this is the only thing standing between the public internet
    and ``/internal/*``.
    """
    token = value.strip()
    if len(token) < 32:
        raise ValueError(
            f"{key} must be at least 32 characters — generate with `openssl rand -hex 32`"
        )
    return token


# What a rotation needs, and the reason it is a field rather than a procedure
# note: the two sides are two droplets deployed minutes apart, so a swap that
# only ever accepts one value 401s every request in that region for the gap
# between them. Accepting the previous value for one deploy turns an outage into
# three ordered, order-independent deploys:
#
#   1. set `previous_internal_token` to the NEW value on both sides   (accept both, send old)
#   2. swap: `internal_token` new, `previous_internal_token` old      (accept both, send new)
#   3. drop `previous_internal_token` on both sides                   (accept new only)
_ROTATION_NOTE = (
    "must differ from internal_token — it is the OTHER value accepted during a "
    "rotation, and setting both to the same string rotates nothing"
)


class PostgresPoolConfig(BaseModel):
    """Pool bounds, shared by both planes.

    ``pool_max_size`` has no default and must be stated. It is a *per process*
    ceiling — the api, each worker, and each live-call job process open their own
    pool from this one number — and nothing sums them against the server's
    ``max_connections``. A default is how that sum silently passes the plan's
    limit and call setup begins raising ``TooManyConnectionsError``, so every
    environment names its own. ``pgbouncer/pgbouncer.ini`` has the arithmetic.

    One number cannot fit every process, though, and the file is shared by all of
    them: ``TALQING_PG_CONTROL_POOL_MAX`` / ``TALQING_PG_DATA_POOL_MAX`` narrow it
    for one container (see :func:`load_settings`).
    """

    pool_min_size: int = 1
    pool_max_size: int

    @field_validator("pool_min_size", "pool_max_size")
    @classmethod
    def _pool_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("pool sizes must be >= 1")
        return value

    @model_validator(mode="after")
    def _max_ge_min(self) -> PostgresPoolConfig:
        if self.pool_max_size < self.pool_min_size:
            raise ValueError("pool_max_size must be >= pool_min_size")
        return self


class ControlPostgresConfig(PostgresPoolConfig):
    """Control plane: tenants, users, orgs, secrets. One database, never sharded.

    A whole DSN rather than host/port/user/password parts, matching the data
    plane. Assembling it here could not express query parameters, so against DO
    Managed Postgres asyncpg fell back to ``sslmode=prefer`` — TLS, but
    unverified, and by omission rather than by choice. ``?sslmode=require``
    belongs in the string.
    """

    dsn: str

    @field_validator("dsn")
    @classmethod
    def _dsn_usable(cls, value: str) -> str:
        return _postgres_dsn(value, "postgres.control.dsn")


class DataPostgresConfig(PostgresPoolConfig):
    """This region's data plane: one database, plus any tenant that has outgrown it.

    ``default_dsn`` is where every tenant lives. There is no per-tenant DSN in
    the control plane any more — every organization exists in every region, so a
    region routes every tenant to its own database and there is nothing left for
    a control-plane column to say.

    ``tenant_overrides`` is how a tenant is moved onto a database of its own, and
    it belongs here rather than in the control plane, which would otherwise hold
    one region's fact about another. Absent means the default, which is every
    tenant until one earns its own database.

    ⚠️ **The config edit does not move data.** A tenant switched to an override
    after it has rows points at an empty database, and the application will
    happily serve that as an empty workspace. Moving a live tenant is a dump, a
    restore and a window — never an edit. Adding one is four ordered steps: a
    ``[databases]`` entry in ``pgbouncer.ini`` (``_through_proxy`` rewrites only
    host and port, so the DATABASE NAME is what routes), then create the
    database, then add the override, then restart — which migrates it on boot.
    """

    default_dsn: str
    tenant_overrides: dict[str, str] = Field(default_factory=dict)

    @field_validator("default_dsn")
    @classmethod
    def _dsn_usable(cls, value: str) -> str:
        return _postgres_dsn(value, "postgres.data.default_dsn")

    @field_validator("tenant_overrides")
    @classmethod
    def _overrides_usable(cls, value: dict[str, str]) -> dict[str, str]:
        from uuid import UUID

        out: dict[str, str] = {}
        for tenant_id, dsn in value.items():
            try:
                key = str(UUID(str(tenant_id)))
            except ValueError:
                raise ValueError(
                    f"postgres.data.tenant_overrides key {tenant_id!r} is not a tenant UUID"
                ) from None
            out[key] = _postgres_dsn(dsn, f"postgres.data.tenant_overrides[{key}]")
        return out

    @property
    def dsns(self) -> list[str]:
        """Every data-plane DSN this region owns, default first.

        The migration runner and the job scheduler both iterate exactly this
        set — see ``db.pool.data_dsns``, which is the one place that reads it, so
        the same query is not pasted into two loops that can then disagree.
        """
        return [self.default_dsn, *sorted(set(self.tenant_overrides.values()) - {self.default_dsn})]


class PostgresConfig(BaseModel):
    """One plane per node: control holds ``control``, a region holds ``data``.

    Both are optional here and required by :meth:`Settings._role_carries_its_own_keys`,
    which is what makes "control never touches a data plane" structural rather
    than a promise — the control droplet's config has no data DSN to reach one
    with, and a region's has no control DSN into the global identity store.
    """

    control: ControlPostgresConfig | None = None
    data: DataPostgresConfig | None = None
    # ``host:port`` of the pgbouncer every pool in this process dials instead of
    # the server named in the DSN. Set from TALQING_PG_PROXY by
    # :func:`load_settings` and not from the config file: which containers have
    # a proxy in front of them is a property of the deployment, and a `psql`
    # from a shell or a script under ``backend/scripts/`` has none. None means
    # "connect directly", which is also the rollback for the whole arrangement.
    proxy: str | None = None

    @field_validator("proxy")
    @classmethod
    def _proxy_host_port(cls, value: str | None) -> str | None:
        if value is None:
            return None
        host, _, port = value.strip().rpartition(":")
        if not host or not port.isdigit():
            raise ValueError(f"postgres.proxy must be host:port, got {value!r}")
        return value.strip()


class RedisConfig(BaseModel):
    # Cluster mode in every environment, so there is no flag here to get wrong.
    # Cluster mode serves database 0 only — the URL must end in /0.
    url: str

    @field_validator("url")
    @classmethod
    def _cluster_url(cls, value: str) -> str:
        url = value.strip()
        if not url.startswith(("redis://", "rediss://")):
            raise ValueError("redis.url must start with redis:// or rediss://")
        # A cluster client asked for any other database raises on the first
        # command, not at connect — so a URL ending in /1 (or in no database at
        # all) surfaces as a mid-call failure rather than a bad config.
        if not url.endswith("/0"):
            raise ValueError("redis.url must end in /0 — cluster mode serves database 0 only")
        return url


class KafkaConfig(BaseModel):
    bootstrap_servers: str
    text_turns_topic: str = "talqing.text.turns.v1"
    text_consumer_group: str = "talqing-text-workers-v1"
    # Deferred work that has come due. Only ever "this job is due now" — the
    # schedule itself lives in `scheduled_jobs`, because a retention purge is
    # ninety days out and a topic is not a database.
    jobs_topic: str = "talqing.jobs.due.v1"
    jobs_consumer_group: str = "talqing-job-executors-v1"

    @field_validator(
        "bootstrap_servers",
        "text_turns_topic",
        "text_consumer_group",
        "jobs_topic",
        "jobs_consumer_group",
    )
    @classmethod
    def _non_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Kafka settings must not be blank")
        return value


class SipDigestCredential(BaseModel):
    """A username/password a SIP peer answers our digest challenge with."""

    username: str = Field(min_length=1)
    password: str = Field(min_length=16)


class LiveKitSipConfig(BaseModel):
    """Self-hosted SIP (telephony). Empty ``uri`` → provision fails until set.

    ``uri`` is the public SIP name of a Kamailio edge, which picks a livekit-sip
    backend per call. This config belongs to ONE region, so it normally holds
    exactly one entry; it stays a list because each telephony account is pinned
    to an entry at creation and keeps it (``assign_sip_uri``), which is what lets
    a second edge name be added without moving existing accounts.
    """

    uri: list[str] = Field(default_factory=list)
    transport: str = "tcp"  # tcp | tls | udp
    # Wall-clock cap on one PSTN call, pushed to LiveKit on both the inbound
    # trunk and CreateSIPParticipant. Left unset, livekit-sip applies its own
    # 24h ceiling (`maxCallDuration` in sip/pkg/sip/participant.go), which bounds
    # nothing that matters: a call with live audio that never hangs up — an
    # off-hook handset, hold music, an IVR talking to the agent — bills
    # STT+LLM+TTS for a day and holds a call slot the whole time. Its 15s media
    # timeout only catches calls that go silent. Keep this inside the voice
    # worker's drain_timeout.
    max_call_duration_seconds: int = 10800
    # Sent alongside the cap above, and note it means two different things.
    # Outbound it is how long we let the callee's phone ring; inbound it is how
    # long the caller waits for the agent to publish audio before livekit-sip
    # gives up (sip/pkg/sip/inbound.go, waitSubscribe). LiveKit's own default is
    # 3 minutes for both, which is far past the point either is worth waiting
    # for: PSTN answers or diverts to voicemail inside 45s, and a caller sitting
    # in silence while an agent fails to start has already had a failed call.
    ringing_timeout_seconds: int = 60
    # What Twilio authenticates a WhatsApp call's SIP leg with. One per region,
    # never per tenant: the leg lands on a single default trunk whose only job is
    # to drop it into the room our voice webhook already made, and the webhook
    # hands Twilio this credential in the `<Sip>` it returns. It keeps scanner
    # INVITEs, which address that trunk as readily as Twilio does, out of rooms.
    # Rotating it takes effect on the next enable of any WhatsApp calls trigger,
    # which re-applies it to the trunk (`livekit_sip.ensure_whatsapp_route`).
    whatsapp: SipDigestCredential

    @field_validator("ringing_timeout_seconds")
    @classmethod
    def _ringing_within_documented_limit(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("livekit.sip.ringing_timeout_seconds must be > 0")
        # LiveKit documents an 80s upper limit on CreateSIPParticipant. The
        # source only rejects <= 0, so the ceiling may be advisory — but one
        # value feeds both directions here, so stay inside the stricter of them.
        if value > 80:
            raise ValueError(
                "livekit.sip.ringing_timeout_seconds must be <= 80 "
                "(LiveKit's documented limit on CreateSIPParticipant)"
            )
        return value

    @field_validator("max_call_duration_seconds")
    @classmethod
    def _duration_within_livekit_ceiling(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("livekit.sip.max_call_duration_seconds must be > 0")
        # livekit-sip silently clamps anything above 24h to 24h, so a larger
        # value here would read as configured and behave as something else.
        if value > 86400:
            raise ValueError(
                "livekit.sip.max_call_duration_seconds must be <= 86400 "
                "(livekit-sip clamps to a 24h ceiling)"
            )
        return value

    @field_validator("uri")
    @classmethod
    def _uris_distinct(cls, value: list[str]) -> list[str]:
        uris = [u.strip().lower() for u in value if u and u.strip()]
        # A repeat would silently double that entry's share of new accounts.
        if len(set(uris)) != len(uris):
            raise ValueError("livekit.sip.uri must not repeat an entry")
        return uris

    @field_validator("transport")
    @classmethod
    def _known_transport(cls, value: str) -> str:
        transport = value.strip().lower()
        if transport not in ("tcp", "tls", "udp"):
            raise ValueError(f"invalid livekit.sip.transport: {value!r}")
        return transport


class LiveKitConfig(BaseModel):
    """LiveKit SFU cluster endpoints.

    - ``url``: private signal used by API/workers (any node or fixed private IP).
    - ``public_urls``: browser ``server_url`` list; API picks one at random on
      ``POST /v1/calls/token`` (``livekit-1`` / ``-2`` / ``-3``; may share one IP).
    """

    url: str  # worker / in-cluster
    public_urls: list[str]
    api_key: str
    api_secret: str
    agent_name: str
    sip: LiveKitSipConfig

    @field_validator("public_urls")
    @classmethod
    def _public_urls_non_empty(cls, value: list[str]) -> list[str]:
        urls = [u.strip() for u in value if u and u.strip()]
        if not urls:
            raise ValueError("livekit.public_urls must be a non-empty list")
        return urls

    # The voice worker hands these straight to the agents SDK, which would
    # otherwise reject a blank deep in its own startup and blame an unset
    # LIVEKIT_* env var. Fail here instead, naming the config key.
    @field_validator("url", "api_key", "api_secret", "agent_name")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("LiveKit settings must not be blank")
        return value

    # The signal endpoint is a WebSocket. An http:// here connects, gets a 200
    # instead of an upgrade, and surfaces as the worker never registering.
    @field_validator("url")
    @classmethod
    def _websocket_url(cls, value: str) -> str:
        if not value.startswith(("ws://", "wss://")):
            raise ValueError("livekit.url must start with ws:// or wss://")
        return value


class ProviderKeyConfig(BaseModel):
    api_key: str = ""


class XaiProviderConfig(ProviderKeyConfig):
    base_url: str = "https://api.x.ai/v1"


class ExaProviderConfig(BaseModel):
    # Required, unlike its siblings: the CoPilots' prompts tell them they can
    # read the web, so a region without the key must not boot and quietly break
    # that promise.
    api_key: str = Field(min_length=1)


class ProvidersConfig(BaseModel):
    xai: XaiProviderConfig
    # The CoPilots' web search and page reading, over Exa's hosted MCP. Not in
    # `Settings.provider_secrets`: nothing builds a model on it.
    exa: ExaProviderConfig
    sarvam: ProviderKeyConfig = Field(default_factory=ProviderKeyConfig)
    raya: ProviderKeyConfig = Field(default_factory=ProviderKeyConfig)
    soniox: ProviderKeyConfig = Field(default_factory=ProviderKeyConfig)
    gemini: ProviderKeyConfig = Field(default_factory=ProviderKeyConfig)
    elevenlabs: ProviderKeyConfig = Field(default_factory=ProviderKeyConfig)
    openai: ProviderKeyConfig = Field(default_factory=ProviderKeyConfig)
    deepgram: ProviderKeyConfig = Field(default_factory=ProviderKeyConfig)
    anam: ProviderKeyConfig = Field(default_factory=ProviderKeyConfig)
    # Deliberately absent from `Settings.provider_secrets` below: noise
    # cancellation is strict BYOK with no gallery and no platform-paid path, so
    # nothing runs on this key. It is here so the license has a home next to the
    # other provider credentials rather than being dropped as an unknown field.
    aicoustics: ProviderKeyConfig = Field(default_factory=ProviderKeyConfig)


class VadConfig(BaseModel):
    """Local Silero VAD for user speech activity / interruption.

    ``min_silence_duration`` is the prewarm seed only. Cascade agents overwrite
    it per call with their own endpointing setting (`compile._configure_vad`);
    realtime agents, whose model detects turns server-side, are the ones that
    actually keep it.
    """

    min_speech_duration: float = 0.1
    min_silence_duration: float = 0.1
    activation_threshold: float = 0.5

    @field_validator("min_speech_duration", "min_silence_duration")
    @classmethod
    def _duration_non_negative(cls, value: float) -> float:
        if value < 0:
            raise ValueError("VAD durations must be >= 0")
        return value

    @field_validator("activation_threshold")
    @classmethod
    def _threshold_range(cls, value: float) -> float:
        if not 0 < value < 1:
            raise ValueError("activation_threshold must be between 0 and 1")
        return value


class BucketConfig(BaseModel):
    """One bucket, with the credentials that can reach it.

    Name and key travel together because DigitalOcean limited-access keys are
    scoped to one bucket — measured, not assumed: the attachments key gets a 403
    against ``talqing-dev-recordings``. A bare bucket name beside one shared
    credential pair would let a caller open a client for one bucket holding the
    other's key, and the only symptom is a 403 in the middle of a call.
    """

    name: str
    access_key: str
    secret_key: str

    @field_validator("name", "access_key", "secret_key")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("bucket settings must not be blank")
        return value


class StorageConfig(BaseModel):
    """DigitalOcean Spaces — every environment, local included.

    Two buckets: call recordings, and the images people attach to a conversation
    (``services.attachments``). Both are private and stay that way — their
    contents are reached only through short-lived presigned URLs the API mints
    per request, which is also why the same endpoint has to work from the API,
    from a worker and from a browser. SigV4 signs the Host header, so there is no
    rewriting a URL for a different address afterwards.

    One ``endpoint_url`` and one ``region`` for both, because they are one Spaces
    account in one region. Everything that differs per bucket — the name and the
    key that can reach it — is inside ``BucketConfig``.

    Local and dev deliberately point at a real Spaces bucket rather than a
    stand-in. MinIO used to fill that role, and two things ruled it out once the
    audio started being served from the bucket: its server answers
    ``PutBucketCors`` with NotImplemented, and — the one that actually settles
    it, because the first has a workaround in ``MINIO_API_CORS_ALLOW_ORIGIN`` —
    the API signs the URL while the browser fetches it, so both must name the
    bucket by the same host. In compose the API sees ``minio:9000`` and a
    browser can only reach ``localhost:9000``, which is a ``SignatureDoesNotMatch``.
    A real bucket is one address for everyone; the price is that local
    development shares the dev buckets and ``down -v`` does not clear them.

    ``endpoint_url`` is required rather than derived, because every provider
    spells its regional endpoint differently and guessing one wrong fails at the
    first upload instead of at boot. ``force_path_style`` must stay true for
    everything that is not AWS itself: Spaces serves browsers path-style, and
    reserves virtual-host style for API access.
    """

    endpoint_url: str
    region: str
    recordings: BucketConfig
    attachments: BucketConfig
    force_path_style: bool = True

    @field_validator("endpoint_url")
    @classmethod
    def _endpoint_url(cls, value: str) -> str:
        url = value.strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("storage.endpoint_url must start with http:// or https://")
        return url.rstrip("/")

    @field_validator("region")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("storage settings must not be blank")
        return value


class SecurityConfig(BaseModel):
    """Keys, and which node is allowed to hold each of them.

    **The control plane mints; every region only verifies.** Sessions and
    personal access tokens are Ed25519 (EdDSA) rather than HS256 for exactly that
    reason: with a shared symmetric key every region has to hold the key it
    verifies with, so every region can also *mint* — and a compromised US droplet
    could sign a session for any ``(user_id, tenant_id)`` it can name and present
    it to the India API, which would accept it. Region isolation is illusory
    while that is true.

    ``session_signing_key`` therefore lives ONLY on the control droplet, and a
    region carrying one is refused at boot. Verification stays a local, offline
    operation with no round trip, which matters more now that there is no cache:
    signature checking must never become a network call.

    ``oauth_state_key`` stays symmetric (HS256) because each node mints and reads
    its own: the Google login state on control, the integration OAuth state on a
    region. Neither crosses a host, and each node generates its own value.
    """

    session_cookie_name: str
    # The registrable domain the session cookie is scoped to. Null means
    # host-only, which is right on localhost and wrong everywhere else: a cookie
    # issued by `api.control.talqing.com` with no domain is never sent to
    # `api.in.talqing.com`, so the region would see every request as anonymous.
    # `talqing.com` also sends it to the marketing site and to every future
    # subdomain — that is the cost, and it is stated here so nobody rediscovers
    # it. SameSite=Lax keeps working because every host shares this domain.
    session_cookie_domain: str | None = None
    # HS256, for state this same process mints and reads back within one host.
    oauth_state_key: str
    # Ed25519 private key, PEM (PKCS#8). CONTROL ONLY — it is what mints every
    # session and PAT in the deployment. Escrow it beside secrets_fernet_key.
    session_signing_key: str = ""
    # Ed25519 public key, PEM (SubjectPublicKeyInfo). Every node carries it;
    # control's must be the half of its own private key, which is checked below.
    session_verify_key: str = ""
    # Fernet — tenant secrets, in-process only. REGION ONLY: a control droplet
    # that cannot decrypt a tenant credential cannot leak one.
    secrets_fernet_key: str = ""
    # HS256, signs the browser chat tokens this region mints and reads back
    # itself (`POST /v1/chats/{id}/token`). REGION ONLY, and its own key rather
    # than a reuse of `oauth_state_key`, so a leaked chat token says nothing
    # about any other signature this node makes.
    chat_token_secret: str = ""
    ssrf_allow_hosts: list[str] = Field(default_factory=list)

    @field_validator("oauth_state_key")
    @classmethod
    def _oauth_state_key_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("oauth_state_key is required for signing OAuth state")
        return value

    @field_validator("session_signing_key")
    @classmethod
    def _signing_key_usable(cls, value: str) -> str:
        # Parsed here rather than at first use: a malformed key would otherwise
        # boot cleanly, pass health checks, and fail on the first login.
        if value.strip():
            _load_ed25519_private(value)
        return value

    @field_validator("session_verify_key")
    @classmethod
    def _verify_key_usable(cls, value: str) -> str:
        if value.strip():
            _load_ed25519_public(value)
        return value

    @model_validator(mode="after")
    def _key_pair_agrees(self) -> SecurityConfig:
        """A private key and a public key that are not halves of one pair would
        401 every authenticated request in the deployment. Catch it at boot."""
        if not (self.session_signing_key.strip() and self.session_verify_key.strip()):
            return self
        derived = _load_ed25519_private(self.session_signing_key).public_key()
        if _public_pem(derived) != _public_pem(_load_ed25519_public(self.session_verify_key)):
            raise ValueError(
                "security.session_verify_key is not the public half of "
                "security.session_signing_key — every session this node mints "
                "would be rejected by every node that verifies it"
            )
        return self

    @field_validator("secrets_fernet_key")
    @classmethod
    def _secrets_fernet_key_usable(cls, value: str) -> str:
        # Building the Fernet here rather than checking for emptiness: crypto.py
        # constructs the cipher lazily on first encrypt/decrypt, so without this
        # an empty or malformed key boots cleanly, passes health checks, serves
        # traffic, and raises the first time a tenant credential is touched —
        # mid-call, on the path that fetches a provider key.
        #
        # Blank is allowed HERE and refused for a region by
        # `Settings._role_carries_its_own_keys`: control has no tenant secrets
        # and must not hold the key to any.
        if not value.strip():
            return value
        try:
            Fernet(value.encode())
        except Exception as exc:
            raise ValueError(
                "secrets_fernet_key is not a valid Fernet key "
                f"(32 url-safe base64-encoded bytes): {exc}"
            ) from exc
        return value


class AppConfig(BaseModel):
    api_public_url: str = "http://localhost:8000"
    dashboard_public_url: str = "http://localhost:3000"
    # REGION ONLY, and required there by `Settings._role_carries_its_own_keys`.
    # It is dialled on every tool call, and control runs no tenant code — so a
    # control config naming a code-exec is a URL for a service that node has no
    # reason to be able to reach, which is exactly the coupling this deployment
    # is shaped to prevent. Blank is how "this node has none" is spelled.
    code_exec_url: str = ""
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    # Local examples often pick a free port; regex avoids editing cors_origins each time.
    cors_origin_regex: str | None = r"^http://(localhost|127\.0\.0\.1):[0-9]+$"

    # api_public_url is handed to third parties as an OAuth redirect and a
    # webhook callback; code_exec_url is dialled on every tool call. A
    # scheme-less value in either fails at use, in someone else's request log.
    # Note this cannot catch an unsubstituted hostname — `http://CODE_EXEC_HOST`
    # is a well-formed URL. That is what the {{TOKEN}} convention is for, and
    # what each bootstrap.sh refuses to ship.
    @field_validator("api_public_url", "dashboard_public_url", "code_exec_url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        url = value.strip()
        # Blank is a real answer for `code_exec_url` alone, and the role check is
        # what makes it one — an empty `api_public_url` still fails here.
        if url and not url.startswith(("http://", "https://")):
            raise ValueError("app URLs must start with http:// or https://")
        return url

    @field_validator("api_public_url", "dashboard_public_url")
    @classmethod
    def _required_url(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("app URLs must start with http:// or https://")
        return value

    @field_validator("cors_origin_regex")
    @classmethod
    def _regex_compiles(cls, value: str | None) -> str | None:
        if value is None:
            return None
        # "" is not a way to switch this off: it compiles, matches nothing, and
        # reads like a disabled setting while being an enabled one. null is.
        if not value.strip():
            raise ValueError("app.cors_origin_regex must be a pattern or null, not an empty string")
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"app.cors_origin_regex is not a valid regex: {exc}") from exc
        return value


class RegionConfig(BaseModel):
    """One region this deployment serves. Control's config holds the whole list.

    **There is deliberately no `regions` table.** The list is *configuration* — it
    changes when we stand a region up, which is a deploy, never at runtime, and
    nothing but us ever writes it. Same reasoning, and the same place, as
    `billing.packs`.

    A region is a place an organization's resources can live. There is no
    enablement and no per-region status: every region serves every organization,
    and the dashboard's switcher offers all of them.
    """

    # A product identifier, never a datacenter — `in`, `us`, `eu`. It is the
    # tail of a region node's ENV and the name in its hostnames.
    slug: str
    # What a person reads in the switcher. `us` is not a label.
    name: str
    # The region's public API base URL. Served to the dashboard by GET /v1/regions,
    # so this is the address a BROWSER uses.
    api_url: str
    # Where CONTROL reaches this region, when that is a different address from
    # the one a browser uses. Blank means `api_url`, which is the production
    # answer — `https://api.in.talqing.com` is one name for everybody.
    #
    # It exists for local compose, where the region's API is `localhost:8000` to
    # the browser and `api:8000` on the bridge, and no single value can be both.
    # Without it the credit push is the one part of this design local cannot
    # rehearse, which is exactly the part worth rehearsing.
    internal_url: str = ""
    # The shared secret for this region's link with control, in BOTH directions:
    # control sends it to push credit, and accepts it from this region on
    # /internal/*. It must equal that region's own `control.internal_token` —
    # see `_internal_token`, which is where that rule is written down.
    internal_token: str
    # The value ALSO accepted, for the length of a rotation. Blank normally.
    previous_internal_token: str = ""
    # ISO 3166-1 alpha-2 codes whose visitors open their first workspace here.
    # A country in no list falls to the default region, which is why an
    # empty list is normal rather than a gap.
    countries: list[str] = Field(default_factory=list)
    # Exactly one region carries this. It is where an organization is created
    # when the visitor's country is unknown, and where the dashboard opens with
    # no stored preference.
    default: bool = False

    @field_validator("slug")
    @classmethod
    def _slug_shape(cls, value: str) -> str:
        slug = value.strip().lower()
        if not _SLUG_RE.match(slug) or slug == CONTROL_NODE:
            raise ValueError(
                f"region slug {value!r} must be lowercase letters, digits and dashes, "
                f"and must not be {CONTROL_NODE!r}"
            )
        return slug

    @field_validator("name")
    @classmethod
    def _name_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a region's name is what a person reads in the switcher")
        return value.strip()

    @field_validator("api_url", "internal_url")
    @classmethod
    def _api_url(cls, value: str) -> str:
        url = value.strip().rstrip("/")
        if url and not url.startswith(("http://", "https://")):
            raise ValueError("a region's URLs must start with http:// or https://")
        return url

    @property
    def control_url(self) -> str:
        """Where control posts to this region."""
        return self.internal_url or self.api_url

    @field_validator("internal_token")
    @classmethod
    def _token(cls, value: str) -> str:
        return _internal_token(value, "regions[].internal_token")

    @field_validator("previous_internal_token")
    @classmethod
    def _previous_token(cls, value: str) -> str:
        return _internal_token(value, "regions[].previous_internal_token") if value.strip() else ""

    @model_validator(mode="after")
    def _rotation_is_a_rotation(self):
        if self.previous_internal_token and self.previous_internal_token == self.internal_token:
            raise ValueError(f"regions[{self.slug}].previous_internal_token {_ROTATION_NOTE}")
        return self

    @property
    def accepted_tokens(self) -> tuple[str, ...]:
        """Every token control accepts from this region right now."""
        return (
            (self.internal_token, self.previous_internal_token)
            if self.previous_internal_token
            else (self.internal_token,)
        )

    @field_validator("countries")
    @classmethod
    def _country_codes(cls, value: list[str]) -> list[str]:
        codes = []
        for raw in value:
            code = raw.strip().upper()
            if len(code) != 2 or not code.isalpha():
                raise ValueError(f"{raw!r} is not an ISO 3166-1 alpha-2 country code")
            codes.append(code)
        return codes


class ControlConfig(BaseModel):
    """How this region reaches the control plane. Regional configs only.

    A region holds a bearer token, never a DSN: it runs tenant-authored tool code
    and already holds the tenant secrets key, so a connection string into the
    global identity store — every user, every organization, every access token —
    does not belong on that box.
    """

    api_url: str
    # The shared secret for this region's link with control, in BOTH directions:
    # sent when calling control, and accepted from control on /internal/*. It
    # must equal control's `regions[<this region>].internal_token`.
    internal_token: str
    # The value ALSO accepted, for the length of a rotation. Blank normally.
    previous_internal_token: str = ""
    # Wall-clock cap on one control round trip. This is on the auth hot path of
    # every authenticated request, so a control plane that has stopped answering
    # has to fail fast rather than hold a worker: the accepted behaviour is "no
    # new requests", not "requests that hang".
    timeout_seconds: float = 5.0

    @field_validator("api_url")
    @classmethod
    def _api_url(cls, value: str) -> str:
        url = value.strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise ValueError("control.api_url must start with http:// or https://")
        return url

    @field_validator("internal_token")
    @classmethod
    def _token(cls, value: str) -> str:
        return _internal_token(value, "control.internal_token")

    @field_validator("previous_internal_token")
    @classmethod
    def _previous_token(cls, value: str) -> str:
        return _internal_token(value, "control.previous_internal_token") if value.strip() else ""

    @model_validator(mode="after")
    def _rotation_is_a_rotation(self):
        if self.previous_internal_token and self.previous_internal_token == self.internal_token:
            raise ValueError(f"control.previous_internal_token {_ROTATION_NOTE}")
        return self

    @property
    def accepted_tokens(self) -> tuple[str, ...]:
        """Every token this region accepts from control right now."""
        return (
            (self.internal_token, self.previous_internal_token)
            if self.previous_internal_token
            else (self.internal_token,)
        )

    @field_validator("timeout_seconds")
    @classmethod
    def _timeout_positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("control.timeout_seconds must be > 0")
        return value


class OAuthClientConfig(BaseModel):
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = ""


class GoogleOAuthConfig(OAuthClientConfig):
    """Shared Google OAuth app: dashboard login + Calendar MCP."""

    login_redirect_uri: str = ""


class RedirectOnlyOAuthConfig(BaseModel):
    """DCR/PKCE providers (Cal.com, Calendly, Jira, RocketReach) — optional redirect override."""

    redirect_uri: str = ""


class OAuthConfig(BaseModel):
    google: GoogleOAuthConfig = Field(default_factory=GoogleOAuthConfig)
    # DCR, not a static client: mcp.cal.com is its own authorization server and
    # registers us dynamically. A Cal.com Platform OAuth app is the wrong
    # credential entirely — its tokens 401 against the MCP server.
    cal_com: RedirectOnlyOAuthConfig = Field(default_factory=RedirectOnlyOAuthConfig)
    calendly: RedirectOnlyOAuthConfig = Field(default_factory=RedirectOnlyOAuthConfig)
    asana: OAuthClientConfig = Field(default_factory=OAuthClientConfig)
    jira: RedirectOnlyOAuthConfig = Field(default_factory=RedirectOnlyOAuthConfig)
    hubspot: OAuthClientConfig = Field(default_factory=OAuthClientConfig)
    rocketreach: RedirectOnlyOAuthConfig = Field(default_factory=RedirectOnlyOAuthConfig)


class BatchCallingConfig(BaseModel):
    """How the batch outbound dispatcher paces itself (background-worker only).

    No discovery interval and no lock TTL: a batch is a `call.batch.dial` job, so
    `scheduled_at` decides when a pass runs and `store.claim` decides which
    container runs it.
    """

    # How long a pass defers its own job before the next one. Costs roughly 14 %
    # of a batch's theoretical throughput — a slot freed at a random moment waits
    # half a pass on average — in exchange for a handler that is trivial to
    # reason about. Shortening it for one batch does not touch any other.
    pass_interval_seconds: int = 30
    # Between claims, so a batch filling ten slots does not hand the trunk ten
    # simultaneous INVITEs; carriers answer a burst with 5xx on numbers that
    # were perfectly good. NOT a measured carrier CPS limit — real trunk limits
    # are higher — and never the thing limiting a batch's throughput, since the
    # most a pass ever needs is `max_concurrency` dials.
    dial_interval_seconds: float = 1.0
    # Last resort around one claim-and-dial. LiveKit's own HTTP deadline
    # (`livekit_sip.LIVEKIT_HTTP_TIMEOUT_SECONDS`) fires well inside this on
    # both RPCs, so the normal degraded path is LiveKit raising and the dial
    # rolling back cleanly; this only catches a hang neither of those covers.
    dial_timeout_seconds: float = 30.0


class EmailOutboundConfig(BaseModel):
    """The shape of an email loop (background-worker only).

    One knob, and it is about OUR INFRASTRUCTURE rather than about anyone's list.
    How many rows draft at once, how fast emails leave and how many may leave in
    a day are the batch's and the run's own columns — properties of the list and
    its sending domain. A second copy of any of those here would be a second
    thing to disagree.
    """

    # The longest a WAITING loop may ignore a change to policy. Both email
    # handlers sleep in chunks of at most this, re-reading the batch or run every
    # turn and, on each chunk, the task's published version (drafting) or the
    # credential (sending) — so raising a daily cap, widening a window,
    # publishing a new prompt or revoking a key all reach a multi-day send within
    # five minutes, with no restart, no signal and nothing to wake.
    #
    # Authorization is NOT on this tick: the creator's membership is checked once
    # per invocation, the way an HTTP request is authorized on arrival.
    #
    # Deliberately polling rather than LISTEN/NOTIFY: an operator who edits a
    # send and watches it change within five minutes has what they came for, and
    # the alternative is a second delivery mechanism to keep correct.
    recheck_seconds: int = 300


class StreamsConfig(BaseModel):
    """The WebSocket media-stream gateway (`api.stream.main`), per region.

    A partner's platform terminates the PSTN leg and connects one socket per call
    to `public_host`. Nothing here is optional in the sense of having a sensible
    fallback: a blank host would build a URL a partner cannot dial, so it is read
    and thrown rather than defaulted at the point of use.
    """

    # The hostname a partner's URL names — `streams.in.talqing.com`. Only used to
    # BUILD the URL we hand out; the gateway itself never reads it, because what
    # it serves is decided by Caddy and the container's port.
    public_host: str = ""

    # Wall-clock cap on one stream call, enforced by the gateway itself. Nothing
    # else in the stack bounds one: `livekit.sip.max_call_duration_seconds` is
    # pushed to livekit-sip and reaches nothing on a socket, so a partner whose
    # stream never closes would bill speech-to-text, LLM and text-to-speech
    # indefinitely and hold a worker slot the whole time. Held at the same 3h as
    # a phone call — one number, one meaning, both channels — and, like it, must
    # stay inside the voice worker's `drain_timeout`.
    max_call_duration_seconds: int = 10800

    # How long the partner's platform waits for our WebSocket handshake before
    # abandoning the call. SparkTG's is 5 s and the failure is invisible to us —
    # no event, no error, just a dropped call in their logs. We cannot enforce
    # this (the deadline is theirs), so it is here to be ALERTED on: the gateway
    # logs a connect that took longer, which is the only warning we will ever get
    # before a partner reports "some calls just don't connect".
    accept_deadline_ms: int = 5000

    @field_validator("public_host")
    @classmethod
    def _bare_host(cls, value: str) -> str:
        host = value.strip().lower()
        # A scheme here would produce `wss://wss://…`, which fails at the
        # partner's end on a URL nobody can read back to a typo.
        if host.startswith(("ws://", "wss://", "http://", "https://")):
            raise ValueError("streams.public_host is a bare hostname, with no scheme")
        return host

    @field_validator("max_call_duration_seconds", "accept_deadline_ms")
    @classmethod
    def _positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("streams durations must be > 0")
        return value


class TelephonyConfig(BaseModel):
    """Platform telephony defaults (SIP E.164 normalization, etc.)."""

    # ISO 3166-1 alpha-2 used when bare national digits lack a country code.
    default_e164_region: str = "IN"
    batch: BatchCallingConfig = Field(default_factory=BatchCallingConfig)
    # The third way a call reaches an agent, beside a browser room and a trunk.
    # Under telephony because that is what it is to a tenant — somebody else's
    # phone call — even though not one line of SIP is involved.
    streams: StreamsConfig = Field(default_factory=StreamsConfig)

    @field_validator("default_e164_region")
    @classmethod
    def _region_code(cls, value: str) -> str:
        region = value.strip().upper()
        if len(region) != 2 or not region.isalpha():
            raise ValueError("default_e164_region must be a 2-letter country code")
        return region


# The purchasable packs. A closed set rather than one built from the config file,
# because it is the request enum of a published operation: `pack` is a Literal
# over these, so an unknown one is a 422 from pydantic and the OpenAPI document
# says the same thing in every environment. What each pack *costs* and which Dodo
# product sells it are the parts that differ between test and live, and those are
# the parts that live in config.
CreditPackId = Literal["usd_20", "usd_50", "usd_100", "usd_200", "usd_500", "usd_1000"]


class CreditPackConfig(BaseModel):
    """One purchasable pack: what it grants, and the Dodo product that sells it.

    `dodo_product_id` differs between test and live mode, which is why a pack is
    configuration rather than a constant. Write `amount` as a quoted YAML string
    so it reaches `Decimal` exactly rather than through a float.
    """

    id: CreditPackId
    amount: Decimal
    dodo_product_id: str

    @model_validator(mode="after")
    def _amount_matches_id(self):
        # The id names the price, so a config where they disagree is selling a
        # $500 pack for $50 or crediting $500 for a $50 payment — and
        # `services/billing/topups.py::_amount_mismatch` would then refuse every
        # purchase of it AFTER the buyer's money was taken.
        expected = Decimal(self.id.removeprefix("usd_"))
        if self.amount != expected:
            raise ValueError(f"pack {self.id} must grant {expected}, not {self.amount}")
        return self


class DodoConfig(BaseModel):
    """Test and live are two separate Dodo environments — different API keys,
    different webhook secrets, different product ids. `mode` derives the host, so
    a live key can never be pointed at the test host by a hand-edited URL."""

    mode: Literal["test", "live"] = "test"
    api_key: str = ""
    webhook_secret: str = ""

    @property
    def base_url(self) -> str:
        return f"https://{self.mode}.dodopayments.com"


class BillingConfig(BaseModel):
    """Prepaid credits: the signup grant, the low-balance warning, and Dodo.

    ``packs`` is in BOTH configs and ``dodo`` is control-only. A region still
    renders the pack list — ``credits.balance_summary`` serves it beside the
    balance — but only control reads ``pack.dodo_product_id``, because only
    control creates a checkout. That is also why "packs listed with no API key"
    is checked per role in :class:`Settings` rather than here: on a region it is
    the correct state.
    """

    # The grant for an organization created at signup, PER REGION — every region
    # grants, so the cost of one signup is this times the number of regions. 0
    # turns it off, and that is the default: an environment nobody configured
    # must not give money away.
    signup_grant_usd: Decimal = Decimal("0")
    low_balance_warning_usd: Decimal = Decimal("2")
    dodo: DodoConfig = Field(default_factory=DodoConfig)
    packs: list[CreditPackConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _packs_distinct(self):
        ids = [pack.id for pack in self.packs]
        if len(ids) != len(set(ids)):
            raise ValueError("billing.packs lists the same pack id twice")
        return self


class Settings(BaseModel):
    """Root config object. Load via ``get_settings()``.

    One model, two shapes. ``role`` says which shape this file is, and
    :meth:`_role_carries_its_own_keys` is what makes the halves required — a
    block optional here is mandatory on the node that needs it and refused on the
    node that must not have it.
    """

    env: EnvName
    # Derived from ENV's tail by `load_settings`, never written in the file.
    # It does NOT select routers: which app is running does that, and the
    # container's command already says which. Its job is the assertion each
    # `main.py` makes at import, so a mis-paired config fails immediately.
    role: NodeRole
    # This node's region slug — `None` on control. Not the same thing as
    # `regions` below, which is control's list of every region there is.
    region: str | None = None
    security: SecurityConfig
    app: AppConfig
    postgres: PostgresConfig
    billing: BillingConfig = Field(default_factory=BillingConfig)
    oauth: OAuthConfig = Field(default_factory=OAuthConfig)

    # ── control only ─────────────────────────────────────────────────────────
    # Every region there is. Configuration, not a table (see RegionConfig).
    regions: list[RegionConfig] = Field(default_factory=list)

    # ── region only ──────────────────────────────────────────────────────────
    control: ControlConfig | None = None
    redis: RedisConfig | None = None
    kafka: KafkaConfig | None = None
    livekit: LiveKitConfig | None = None
    storage: StorageConfig | None = None
    providers: ProvidersConfig | None = None
    vad: VadConfig = Field(default_factory=VadConfig)
    telephony: TelephonyConfig = Field(default_factory=TelephonyConfig)
    email: EmailOutboundConfig = Field(default_factory=EmailOutboundConfig)

    @field_validator("env", mode="before")
    @classmethod
    def _normalize_env(cls, value: Any) -> str:
        env = str(value).strip().lower()
        if env not in _VALID_ENVS:
            raise ValueError(f"env must be one of {sorted(_VALID_ENVS)}; got {value!r}")
        return env

    @field_validator("regions")
    @classmethod
    def _region_list_is_coherent(cls, value: list[RegionConfig]) -> list[RegionConfig]:
        """Three invariants, none of which has a cheap SQL equivalent — which is
        part of why the list is config rather than a table."""
        if not value:
            return value
        slugs = [r.slug for r in value]
        if len(set(slugs)) != len(slugs):
            raise ValueError("regions lists the same slug twice")
        defaults = [r.slug for r in value if r.default]
        if len(defaults) != 1:
            raise ValueError(
                "exactly one region must be marked `default: true` — it is where an "
                f"organization is created when the visitor's country is unknown; got {defaults}"
            )
        seen: dict[str, str] = {}
        for region in value:
            for country in region.countries:
                if country in seen:
                    raise ValueError(
                        f"country {country} is claimed by both {seen[country]} and {region.slug}"
                    )
                seen[country] = region.slug
        return value

    @model_validator(mode="after")
    def _no_live_payments_on_a_laptop(self):
        if self.env == "local" and self.billing.dodo.mode == "live":
            raise ValueError(
                "billing.dodo.mode is 'live' in the local config — a laptop must "
                "never be able to take a real payment"
            )
        return self

    @model_validator(mode="after")
    def _role_carries_its_own_keys(self):
        """Each node must hold exactly its own half of the config.

        Absence is checked as hard as presence, and that is the point: a control
        droplet with no data DSN cannot reach a data plane by mistake, and a
        region with no signing key cannot mint a credential for another region.
        Both are structural properties of the deployment here rather than
        promises made somewhere in the code.
        """
        control_only = {"regions": self.regions, "postgres.control": self.postgres.control}
        region_only = {
            "control": self.control,
            "postgres.data": self.postgres.data,
            "redis": self.redis,
            "kafka": self.kafka,
            "livekit": self.livekit,
            "storage": self.storage,
            "providers": self.providers,
            # Tenant tool code runs in a region and nowhere else, so the address
            # of the sandbox that runs it belongs to a region and nowhere else.
            "app.code_exec_url": self.app.code_exec_url,
        }
        present, absent = (
            (control_only, region_only) if self.role == "control" else (region_only, control_only)
        )
        missing = sorted(key for key, value in present.items() if not value)
        if missing:
            raise ValueError(
                f"a {self.role} config must set: {', '.join(missing)} "
                f"(see configs/example.{self.role}.config.yaml)"
            )
        surplus = sorted(key for key, value in absent.items() if value)
        if surplus:
            raise ValueError(
                f"a {self.role} config must NOT set: {', '.join(surplus)} — "
                "that belongs to the other plane, and holding it is the coupling "
                "this deployment is shaped to prevent"
            )

        if self.role == "control":
            if not self.security.session_signing_key:
                raise ValueError(
                    "security.session_signing_key is required on control — it is what "
                    "mints every session and personal access token in the deployment"
                )
            if self.billing.packs and not self.billing.dodo.api_key:
                raise ValueError(
                    "billing.packs are offered but billing.dodo.api_key is blank — "
                    "control is the only node that can sell them"
                )
            # Google is the only way into the product — there is no password
            # fallback — and control is the only node that runs the login. An
            # empty client boots, passes /health and 503s the first person who
            # tries to sign in, which is a deployment nobody can enter. Local is
            # exempt: an empty client there is a contributor running the API
            # without a Google project, not a deployment.
            if self.env != "local" and not (
                self.oauth.google.client_id and self.oauth.google.client_secret
            ):
                raise ValueError(
                    "oauth.google.client_id and client_secret are required on a deployed "
                    "control plane — Google sign-in is the only way into the product"
                )
        else:
            if self.security.session_signing_key:
                raise ValueError(
                    "security.session_signing_key is set on a region — a region that can "
                    "MINT a session can forge one for any other region, which is the "
                    "whole reason these are Ed25519 rather than a shared HS256 key"
                )
            if not self.security.secrets_fernet_key:
                raise ValueError("security.secrets_fernet_key is required on a region")
            if not self.security.chat_token_secret.strip():
                raise ValueError("security.chat_token_secret is required on a region")
            if self.billing.dodo.api_key or self.billing.dodo.webhook_secret:
                raise ValueError(
                    "billing.dodo credentials are set on a region — checkout and the "
                    "webhook are control's, so the payment key lives in one place"
                )
        if not self.security.session_verify_key:
            raise ValueError(
                "security.session_verify_key is required on every node — without it "
                "no session or personal access token can be verified"
            )
        return self

    # ── convenience derived values ───────────────────────────────────────────

    @property
    def default_region(self) -> RegionConfig:
        """Where an organization is created when nothing else decides. Control only."""
        return next(region for region in self.regions if region.default)

    def region_by_slug(self, slug: str) -> RegionConfig | None:
        return next((region for region in self.regions if region.slug == slug), None)

    def region_for_country(self, country: str | None) -> RegionConfig:
        """The region a visitor from ``country`` starts in.

        The COUNTRY is what crosses the wire from the dashboard, not a region
        slug, so pointing a new region at the EEA is one edit here and not a
        dashboard deploy. An unknown or absent country is the normal case, not an
        error: it falls to the default.
        """
        if country:
            code = country.strip().upper()
            for region in self.regions:
                if code in region.countries:
                    return region
        return self.default_region

    @property
    def provider_secrets(self) -> dict[str, str]:
        """Talqing's own provider keys — for what Talqing itself pays for.

        Agent runs are strict BYOK and compile from the tenant's keys
        (``services.byok``); nothing here backstops them. These pay for the
        CoPilots outright, and for the voice/avatar galleries only
        until a tenant stores a key of their own — from then on the gallery
        browses theirs (``services.catalog.voices.resolve_credential``).
        """
        p = self.providers
        return {
            "xai": p.xai.api_key,
            "sarvam": p.sarvam.api_key,
            "raya": p.raya.api_key,
            "soniox": p.soniox.api_key,
            "gemini": p.gemini.api_key,
            "elevenlabs": p.elevenlabs.api_key,
            "openai": p.openai.api_key,
            "deepgram": p.deepgram.api_key,
            "anam": p.anam.api_key,
        }

    @property
    def ssrf_allow_host_set(self) -> frozenset[str]:
        return frozenset(h.strip().lower() for h in self.security.ssrf_allow_hosts if h.strip())


# ── load helpers ─────────────────────────────────────────────────────────────


def _parse_env_file_for_env(path: Path) -> str | None:
    if not path.is_file():
        return None
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() != "ENV":
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value.strip() or None
    return None


def _resolve_selector() -> tuple[EnvName, str]:
    """``ENV`` split into (environment, node): ``prod.in`` -> ``("prod", "in")``.

    One variable to ship and one to get wrong. The head is validated as it always
    was; the tail names the node and decides the filename, the role and — for a
    region — the slug.
    """
    raw = (os.environ.get("ENV") or "").strip()
    if not raw:
        for candidate in (Path(".env"), _BACKEND_ROOT / ".env"):
            raw = _parse_env_file_for_env(candidate) or ""
            if raw:
                break
    if not raw:
        raise ValueError(
            "ENV is required and names both the environment and the node "
            "(e.g. local.control, dev.in, prod.us). Set it in backend/.env or "
            "the process environment."
        )
    env, _, node = raw.strip().lower().partition(".")
    if env not in _VALID_ENVS or not node:
        raise ValueError(
            f"ENV must be '<env>.<node>' with env one of {sorted(_VALID_ENVS)} and node "
            f"either '{CONTROL_NODE}' or a region slug (e.g. prod.in); got {raw!r}"
        )
    if node != CONTROL_NODE and not _SLUG_RE.match(node):
        raise ValueError(
            f"ENV node {node!r} must be '{CONTROL_NODE}' or a region slug "
            "(lowercase letters, digits and dashes)"
        )
    return env, node  # type: ignore[return-value]


def _resolve_config_path(env: str, node: str) -> Path:
    override = (os.environ.get("TALQING_CONFIG_FILE") or "").strip()
    if override:
        path = Path(override)
        if not path.is_file():
            raise FileNotFoundError(f"TALQING_CONFIG_FILE not found: {path}")
        return path

    name = f"{env}.{node}.config.yaml"
    candidates = (
        _CONFIGS_DIR / name,
        Path("configs") / name,
    )
    for path in candidates:
        if path.is_file():
            return path
    searched = ", ".join(str(p.resolve()) for p in candidates)
    # The good failure this naming buys: a `prod.us.config.yaml` mis-copied onto
    # the India droplet is not loaded and wrong, it is not found — and this is
    # the sentence that says so.
    raise FileNotFoundError(
        f"Config file for ENV={env}.{node!r} not found. Looked for: {searched}. "
        f"Create configs/{name} from configs/"
        f"example.{'control' if node == CONTROL_NODE else 'region'}.config.yaml."
    )


def load_settings(env: str | None = None, *, path: Path | None = None) -> Settings:
    """Load and validate ``configs/{env}.{node}.config.yaml`` into ``Settings``.

    ``env`` may be given as the whole selector (``"dev.in"``) — tests and the
    OpenAPI export pass it that way.
    """
    if env is None:
        env_name, node = _resolve_selector()
    else:
        env_name, _, node = env.strip().lower().partition(".")
        if not node:
            raise ValueError(f"env must be '<env>.<node>' (e.g. 'local.in'); got {env!r}")
    config_path = path or _resolve_config_path(env_name, node)
    with config_path.open() as fh:
        raw = yaml.safe_load(fh)
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"{config_path} must be a YAML mapping at the top level")

    data = dict(raw)
    # Belt and braces on top of the filename, which is the real first line of
    # defence: the file states which environment and which node it is for, and a
    # disagreement with the selector is a refusal rather than a surprise.
    file_env = data.get("env")
    if file_env is not None and str(file_env).strip().lower() != env_name:
        raise ValueError(
            f"{config_path} has env={file_env!r} but ENV={env_name!r}; they must match"
        )
    file_region = data.get("region")
    expected_region = None if node == CONTROL_NODE else node
    if file_region is not None and str(file_region).strip().lower() != (expected_region or ""):
        raise ValueError(
            f"{config_path} has region={file_region!r} but ENV names node {node!r}; they must match"
        )
    data["env"] = env_name
    data["role"] = "control" if node == CONTROL_NODE else "region"
    data["region"] = expected_region
    _apply_pool_overrides(data)
    _apply_pg_proxy(data)
    return Settings.model_validate(data)


def _apply_pool_overrides(data: dict) -> None:
    """Let one container narrow its own Postgres ceiling.

    ``postgres.*.pool_max_size`` is per process and the config file is shared by
    every process in the environment, so without this `job-scheduler` — which
    needs about two connections — reserves whatever the api was sized for, and
    every replica added reserves it again. How many connections one container
    needs is a property of the deployment rather than of the environment, so it
    is set on the container and not in YAML. Absent means "whatever the file
    says"; there is no default here to disagree with the file.

    A plane this node does not have is skipped rather than conjured: one .env is
    shared by every container in an environment, so a region reading
    ``TALQING_PG_CONTROL_POOL_MAX`` must not grow a control block from it.
    """
    for plane, var in (
        ("control", "TALQING_PG_CONTROL_POOL_MAX"),
        ("data", "TALQING_PG_DATA_POOL_MAX"),
    ):
        value = (os.environ.get(var) or "").strip()
        if not value:
            continue
        if not value.isdigit():
            raise ValueError(f"{var} must be a positive integer, got {value!r}")
        block = (data.get("postgres") or {}).get(plane)
        if isinstance(block, dict):
            block["pool_max_size"] = int(value)


def _apply_pg_proxy(data: dict) -> None:
    """Point this container's pools at pgbouncer.

    ``TALQING_PG_PROXY=host:port`` makes every pool the process opens dial the
    proxy instead of the server its DSN names — see ``db.pool``. Same reasoning
    as the pool overrides above: which containers sit behind a proxy is a
    property of the deployment, not of the environment whose one config file
    they all read, and unsetting it is how the whole arrangement is rolled back.
    """
    value = (os.environ.get("TALQING_PG_PROXY") or "").strip()
    if value:
        data.setdefault("postgres", {})["proxy"] = value


_cached: Settings | None = None


def get_settings() -> Settings:
    """Return process-wide settings (loaded once from YAML)."""
    global _cached
    if _cached is None:
        _cached = load_settings()
    return _cached


def configure_settings(settings: Settings) -> None:
    """Install settings explicitly (tests / openapi export)."""
    global _cached
    _cached = settings


def clear_settings_cache() -> None:
    """Drop cached settings so the next ``get_settings()`` reloads YAML."""
    global _cached
    _cached = None
