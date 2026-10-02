"""asyncpg connection pools for control-plane and tenant data-plane DBs.

Public surface: ``from db import control_pool, tenant_pool, data_pool, …``.
"""

from db.pool import (
    close_all,
    control_pool,
    data_dsns,
    data_pool,
    data_pool_for_dsn,
    tenant_pool,
    tenant_pool_for_id,
)

__all__ = [
    "close_all",
    "control_pool",
    "data_dsns",
    "data_pool",
    "data_pool_for_dsn",
    "tenant_pool",
    "tenant_pool_for_id",
]
