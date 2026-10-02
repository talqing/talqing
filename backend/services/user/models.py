"""Control-plane identity rows (organization + user)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

# The three organization roles. `control.memberships.role` and
# `control.org_invites.role` are both CHECK-constrained to exactly this set,
# which is why a response is allowed to state it rather than saying `str`.
OrgRole = Literal["ADMIN", "EDITOR", "VIEWER"]


class Tenant(BaseModel):
    """An organization. `tenant` is the internal name; users read "organization".

    It carries no data-plane DSN. Every organization exists in every region, so a
    region routes every tenant to its own database and there is nothing left for
    a control-plane column to say — see ``db.tenant_pool``, whose signature is
    unchanged because the routing moved rather than the call sites.
    """

    id: UUID
    name: str
    # How long this organization keeps its call content. None = forever, which
    # is the default. It rides on the tenant rather than being looked up because
    # session finalize reads it on every call, and this row is already loaded
    # there.
    retention_days: int | None = None
    created_at: datetime | None = None


class User(BaseModel):
    """A person, plus the role they hold in the organization this request is for.

    `role` comes from the ``memberships`` row, not from the user — the same
    person can be an ADMIN in one organization and a VIEWER in another.
    """

    id: UUID
    email: str
    role: OrgRole  # drives the write-access guards
    name: str | None = None
    picture_url: str | None = None
    google_sub: str | None = None
    created_at: datetime | None = None
