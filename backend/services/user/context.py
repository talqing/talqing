"""Signed-in user + tenant context shared by API, services, and CoPilot.

HTTP auth resolution lives in api.{control,dataplane}.deps; this module only defines the
payload type so domain code does not import FastAPI.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import asyncpg

import db
from services.control import client as control

from .models import OrgRole, Tenant, User

# The `OrgRole` values, as constants, for the guards that compare rather than type.
ROLE_ADMIN: OrgRole = "ADMIN"  # everything, incl. members, invites, secrets and provider keys
ROLE_EDITOR: OrgRole = "EDITOR"  # read + write everything except secrets and provider keys
ROLE_VIEWER: OrgRole = "VIEWER"  # read, plus starting a web call to test an agent
ROLES: tuple[OrgRole, ...] = (ROLE_ADMIN, ROLE_EDITOR, ROLE_VIEWER)


@dataclass
class Context:
    user: User  # `user.role` is their role in `tenant`, not a property of the person
    tenant: Tenant  # the organization this request acts in, in THIS region

    async def tenant_pool(self) -> asyncpg.Pool:
        return await db.tenant_pool(self.tenant)


class NotAMember(RuntimeError):
    """This user is not — or is no longer — a member of this organization.

    Distinct from a control-plane query that *errors*, which callers must treat
    as transient and retry. This one is permanent: the work it was authorizing
    must stop, not wait.
    """


async def load_context(tenant: Tenant, user_id: UUID) -> Context:
    """Rebuild a signed-in ``Context`` for queued or scheduled work.

    Deferred work — a CoPilot turn off the Kafka topic, a batch the dispatcher
    picked up an hour after it was created — names the person who asked for it
    and nothing more. This turns that id back into the identity it will act as.

    **The membership check is the authorization check**, exactly as in
    ``api.dataplane.deps``: someone since removed from the organization, or
    demoted, must not still have their queued work run with the access they had
    when they queued it. A refusal from control raises :class:`NotAMember`;
    anything else propagates untouched, because "no longer a member" and "the
    control plane is unreachable" are opposite verdicts and only one of them is
    final. That is the whole reason ``services.control.client`` raises two
    different exceptions.
    """
    try:
        resolved = await control.auth_context(user_id=str(user_id), tenant_id=str(tenant.id))
    except control.ControlRefused as exc:
        raise NotAMember(f"user {user_id} is not a member of this organization") from exc
    # The tenant the caller already holds, not the one control just returned: the
    # caller's is what the surrounding work is scoped to, and the two differ only
    # in freshness.
    return Context(user=resolved.user, tenant=tenant)
