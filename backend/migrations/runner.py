"""Idempotent migration runner.

**Migrations split with the planes.** A control-plane process applies
``migrations/control/`` and nothing else; a region applies ``migrations/data/``
to every DSN it owns. Neither can reach the other's database — control has no
data DSN and a region has no control DSN — so this is enforced by configuration
rather than by a flag, and the two entry points below exist so a process asks for
the half it can actually apply.

Which data DSNs "every one it owns" means comes from ``db.data_dsns()``, i.e.
this region's own config. It used to be ``SELECT DISTINCT db_dsn FROM tenants``,
which reads a globally-shared table: with two regions every boot would try to
migrate the other region's database, and the migration lock is region-local, so
two regions could race the same remote database.

Run as:

    python -m migrations.runner          # migrate whichever plane this node has
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import asyncpg

import db
from settings import get_settings

logger = logging.getLogger("talqing.migrations")

_HERE = Path(__file__).resolve().parent
CONTROL_DIR = _HERE / "control"
DATA_DIR = _HERE / "data"

# Redis coordination for concurrent process startups (API + workers).
_LOCK_KEY = "talqing:migrations"
_LOCK_TIMEOUT_SECONDS = 300
# After a successful migrate pass, skip re-running for this window so every other
# container that boots in the same deploy only waits for the lock, not the SQL.
_DONE_KEY = "talqing:migrations:done"
_DONE_TTL_SECONDS = 300


async def _ensure_tracking(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename   TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


async def _apply_dir(conn: asyncpg.Connection, directory: Path) -> list[str]:
    await _ensure_tracking(conn)
    applied = {r["filename"] for r in await conn.fetch("SELECT filename FROM schema_migrations")}
    newly: list[str] = []
    for path in sorted(directory.glob("*.sql")):
        if path.name in applied:
            continue
        sql = path.read_text()
        async with conn.transaction():
            await conn.execute(sql)
            await conn.execute("INSERT INTO schema_migrations(filename) VALUES($1)", path.name)
        newly.append(path.name)
        logger.info("applied migration %s", path.name)
    return newly


async def _apply_to_dsn(dsn: str, directory: Path) -> list[str]:
    conn = await asyncpg.connect(dsn)
    try:
        return await _apply_dir(conn, directory)
    finally:
        await conn.close()


async def migrate_control() -> None:
    """Apply ``migrations/control/`` to the control-plane database.

    No Redis lock: the control droplet runs one API process against one database,
    so there is nothing to serialize — and it has no Redis of its own (that is a
    regional service). The SQL is idempotent regardless.
    """
    cfg = get_settings().postgres.control
    if cfg is None:
        raise RuntimeError("migrate_control() on a node with no control database")
    applied = await _apply_to_dsn(cfg.dsn, CONTROL_DIR)
    logger.info("migrated the control plane (%d new migration(s))", len(applied))


async def migrate_data() -> None:
    """Apply ``migrations/data/`` to every data DSN this region owns.

    Any regional process may call this (API or workers). A Redis lock serializes
    the work so concurrent startups do not race. Only the first process to
    acquire the lock applies migrations; it then sets a short-lived done key.
    Later processes that acquire the lock see that key and release immediately
    without re-running SQL (which is still idempotent if the key has expired).
    """
    from iredis.client import get_redis

    redis = get_redis()
    async with redis.lock(_LOCK_KEY, timeout=_LOCK_TIMEOUT_SECONDS):
        if await redis.get(_DONE_KEY):
            logger.info("migrations already applied recently; skipping")
            return
        dsns = db.data_dsns()
        for dsn in dsns:
            await _apply_to_dsn(dsn, DATA_DIR)
        await redis.set(_DONE_KEY, "1", ex=_DONE_TTL_SECONDS)
        logger.info("migrated %s data db(s)", len(dsns))


async def migrate_this_node() -> None:
    """Migrate whichever plane this process has. The CLI entry point."""
    if get_settings().role == "control":
        await migrate_control()
    else:
        await migrate_data()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(migrate_this_node())
