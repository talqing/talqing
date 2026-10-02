"""asyncpg connection management.

- control_pool(): shared pool to the control-plane DB (tenants, users, PATs).
  **Control-plane processes only.** A region holds no control DSN — it reaches
  the control plane over HTTP (``services.control.client``) — so calling this
  there is a configuration error, not a connection that happens to fail.
- tenant_pool(tenant): pool for that tenant's data-plane DSN in THIS region.
  Routing comes from the region's own config, never from the control plane.
  Tenants on the same DSN share one pool; isolation is still by a `tenant_id`
  column on every data-plane row and a `tenant_id = ...` filter on every query.
- data_pool(): the default data-plane pool, used only for global maintenance
  paths that intentionally do not have a tenant object.
- data_dsns(): every data-plane DSN this region owns. The migration runner and
  the job scheduler both iterate exactly this, and it is defined once so they
  cannot come to disagree about what "every tenant database" means.

Every pool here dials pgbouncer instead of the server its DSN names when the
container sets TALQING_PG_PROXY (settings ``postgres.proxy``). Nothing else in
the backend knows about the proxy, and nothing may assume it is there — unset
that variable and these pools connect directly again, which is the rollback for
the whole arrangement.
"""

from __future__ import annotations

import asyncio
import json
import logging
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg

from settings import get_settings
from settings.settings import PostgresPoolConfig

logger = logging.getLogger("talqing.db")

# asyncpg's own default, restated because the proxy branch below has to name a
# number and the two are only meaningful side by side.
_STATEMENT_CACHE_DIRECT = 100


def _jsonb_encode(value) -> str:
    # Encode for a json/jsonb param. Most call sites already hand us serialized
    # JSON text (json.dumps(...) / model_dump_json()); pass that straight through.
    # A dict/list is serialized here, so callers may also pass Python objects.
    return value if isinstance(value, str) else json.dumps(value)


async def _init_conn(conn: asyncpg.Connection) -> None:
    """Decode json/jsonb to Python objects on read and accept Python objects
    (or already-serialized text) on write — so route code never json.loads/dumps
    around the DB boundary."""
    for typ in ("jsonb", "json"):
        await conn.set_type_codec(
            typ,
            encoder=_jsonb_encode,
            decoder=json.loads,
            schema="pg_catalog",
            format="text",
        )


def _through_proxy(dsn: str, proxy: str) -> str:
    """Rewrite a DSN to reach Postgres through ``proxy`` (``host:port``).

    Host and port are all that change. The DATABASE NAME is deliberately kept:
    it is pgbouncer's routing key, so its ``[databases]`` section — and not
    anything here — decides which server the connection reaches, and a DSN
    naming a database with no entry there gets a clear pgbouncer error rather
    than silently landing on the wrong one. User and password are kept because
    pgbouncer's ``userlist.txt`` authenticates against them.

    ``sslmode`` is dropped: the proxy's listener does no TLS, and it opens its
    own encrypted connection to the server (``server_tls_sslmode``).
    """
    parts = urlsplit(dsn)
    userinfo, at, _ = parts.netloc.rpartition("@")
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if k != "sslmode"])
    return urlunsplit((parts.scheme, f"{userinfo}{at}{proxy}", parts.path, query, parts.fragment))


async def _create_pool(dsn: str, cfg: PostgresPoolConfig) -> asyncpg.Pool:
    """Open a pool, through this container's proxy if it has one."""
    proxy = get_settings().postgres.proxy
    return await asyncpg.create_pool(
        _through_proxy(dsn, proxy) if proxy else dsn,
        min_size=cfg.pool_min_size,
        max_size=cfg.pool_max_size,
        init=_init_conn,
        # asyncpg names its cached statements and reuses them by name, and under
        # pgbouncer's transaction pooling the backend can change between
        # transactions — so the second use may find nothing by that name. 0 makes
        # asyncpg fall through to the UNNAMED prepared statement, whose Parse,
        # Bind and Execute are flushed together inside one implicit transaction
        # that pgbouncer cannot swap a backend underneath. A direct connection
        # keeps its cache; this costs a re-Parse per call and is the starting
        # position, not a permanent one — pgbouncer 1.25 tracks protocol-level
        # prepared statements itself (max_prepared_statements, default 200), so
        # turning the cache back on is a measured follow-up.
        statement_cache_size=0 if proxy else _STATEMENT_CACHE_DIRECT,
    )


_control_pool: asyncpg.Pool | None = None
# Keyed by the ORIGINAL DSN, never the rewritten one: two tenants on genuinely
# different servers would otherwise collapse onto one pool the moment both
# rewrote to the same proxy address.
_data_pools: dict[str, asyncpg.Pool] = {}
_lock = asyncio.Lock()


async def control_pool() -> asyncpg.Pool:
    global _control_pool
    if _control_pool is None:
        async with _lock:
            if _control_pool is None:
                cfg = get_settings().postgres.control
                if cfg is None:
                    raise RuntimeError(
                        "this node has no control-plane database: a region reaches control "
                        "over HTTP (services.control.client), and a DSN into the global "
                        "identity store deliberately does not exist here"
                    )
                _control_pool = await _create_pool(cfg.dsn, cfg)
    return _control_pool


def _data_config():
    cfg = get_settings().postgres.data
    if cfg is None:
        raise RuntimeError(
            "this node has no data plane: the control plane never opens one, "
            "which is what makes that a property of the deployment rather than a promise"
        )
    return cfg


async def data_pool_for_dsn(dsn: str) -> asyncpg.Pool:
    if not dsn:
        raise ValueError("data-plane DSN is required")
    pool = _data_pools.get(dsn)
    if pool is None:
        async with _lock:
            pool = _data_pools.get(dsn)
            if pool is None:
                pool = await _create_pool(dsn, _data_config())
                _data_pools[dsn] = pool
    return pool


async def data_pool() -> asyncpg.Pool:
    """Default data-plane pool for non-tenant-specific maintenance paths."""
    return await data_pool_for_dsn(_data_config().default_dsn)


def data_dsns() -> list[str]:
    """Every data-plane DSN this region owns — the default, plus any override.

    One helper, two readers: ``migrations.runner`` migrates each of these on boot
    and ``services.jobs.scheduler`` polls each for due work. It used to be
    ``SELECT DISTINCT db_dsn FROM tenants``, which with two regions would have had
    each one trying to migrate and schedule against the *other* region's database
    — unreachable at best, and two regions racing one remote database at worst.
    Scoping it to this region's own config is the same behaviour with the blast
    radius the design already promises.
    """
    return _data_config().dsns


async def tenant_pool_for_id(tenant_id) -> asyncpg.Pool:
    """The pool this tenant's rows live in, in THIS region, from its id alone.

    Every organization exists in every region and there is no enablement table,
    so a region routes every tenant to its own ``default_dsn`` — routing stopped
    coming from the control plane along with ``tenants.db_dsn``. A tenant that has
    outgrown the shared database is named in this region's
    ``postgres.data.tenant_overrides``, which is where such a fact belongs: on the
    region, not in a control plane that would then hold one region's fact about
    another.

    The id is the whole input, which is why this exists beside
    :func:`tenant_pool`: the credit push arrives from control with a tenant id and
    no ``Tenant``, and fetching one would be a round trip BACK to control for a
    row it already had.
    """
    cfg = _data_config()
    return await data_pool_for_dsn(cfg.tenant_overrides.get(str(tenant_id), cfg.default_dsn))


async def tenant_pool(tenant) -> asyncpg.Pool:
    """The pool for a tenant a caller already holds. Routing is by id — the rest
    of the ``Tenant`` was never consulted."""
    return await tenant_pool_for_id(tenant.id)


async def close_all() -> None:
    global _control_pool
    if _control_pool is not None:
        await _control_pool.close()
        _control_pool = None
    for pool in list(_data_pools.values()):
        await pool.close()
    _data_pools.clear()
