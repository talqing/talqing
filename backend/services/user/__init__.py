"""Identity: organization, user, and signed-in request context.

Import what you need from here::

    from services.user import Context, Tenant, User, ROLE_ADMIN, create_organization

Submodules are package-internal; external callers should not import them.
HTTP auth resolution lives in ``api.control.deps`` and ``api.dataplane.deps``;
this package holds the shared payload types so domain code does not import
FastAPI.

Two halves, and which one a caller may use depends on which node it runs on:
``models`` and ``context`` are shared, ``provisioning`` writes the control-plane
tables and runs only there, and ``tenants`` reads them over HTTP and runs only on
a region.
"""

from __future__ import annotations

from .context import (  # noqa: F401
    ROLE_ADMIN,
    ROLE_EDITOR,
    ROLE_VIEWER,
    ROLES,
    Context,
    NotAMember,
    load_context,
)
from .models import OrgRole, Tenant, User  # noqa: F401
from .provisioning import (  # noqa: F401
    MCP_TOKEN_NAME,
    TENANT_COLUMNS,
    add_member,
    create_organization,
    provision_mcp_token,
)
from .tenants import load_tenant  # noqa: F401
