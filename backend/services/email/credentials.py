"""From a batch's `integration_id` to a live API key.

The same credential an agent's Resend MCP server uses, reached the same way:
``integrations.credentials_ref`` holds a ``{{secrets.NAME}}`` ref, and the
secrets service is what turns it into a value in process. Two unrelated uses of
one credential, deliberately — an agent may call Resend's MCP tools, and this
sends a reviewed batch. Neither knows about the other.
"""

from __future__ import annotations

from uuid import UUID

import asyncpg

from services.integrations import INTEGRATION_COLUMNS, Integration, integration_from_row
from services.secrets import MissingSecretError, load_secrets, resolve_secretish
from services.user import Tenant

from .provider import EmailProvider
from .providers import EMAIL_PROVIDERS, require_email_provider


class EmailCredentialError(RuntimeError):
    """The batch's email account is gone, inactive, or has no usable key.

    Ours to tell the tenant about: every one of these is something they changed
    and can change back, which is why a batch that hits one stops with this
    sentence rather than retrying it five more times.
    """


async def load_email_integration(
    conn: asyncpg.Connection | asyncpg.Pool, tenant_id: UUID, integration_id: UUID
) -> Integration | None:
    """The integration row, or ``None``. Takes a connection or a pool."""
    row = await conn.fetchrow(
        f"SELECT {INTEGRATION_COLUMNS} FROM integrations WHERE id = $1 AND tenant_id = $2",
        integration_id,
        tenant_id,
    )
    return integration_from_row(row) if row else None


def check_sendable(integration: Integration) -> None:
    """Raise unless this integration is one a batch may send through."""
    if integration.provider not in EMAIL_PROVIDERS:
        raise EmailCredentialError(
            f"{integration.display_name} is a {integration.provider} integration and cannot "
            "send email"
        )
    if integration.status != "active":
        raise EmailCredentialError(
            f"the {integration.display_name} integration is {integration.status} — "
            "reconnect it before sending"
        )


async def resolve_email_credential(
    tenant: Tenant, integration: Integration
) -> tuple[EmailProvider, str]:
    """``(provider, api key)`` for this integration, or raise saying why not.

    **Everything that can go wrong here leaves as an `EmailCredentialError`**,
    including a `{{secrets.NAME}}` ref whose secret has been deleted. That is
    what the class is for: an account-level fact the tenant caused and can undo.
    Letting `MissingSecretError` through instead made a deleted secret an
    unclassified crash — the send job burned five retries on it and left the rows
    it had queued with no run to hand them back.
    """
    check_sendable(integration)
    secrets = await load_secrets(tenant)
    try:
        credential = resolve_secretish(integration.credentials_ref, secrets)
    except MissingSecretError as exc:
        raise EmailCredentialError(
            f"the {integration.display_name} integration points at the secret "
            f"'{exc.name}', which no longer exists — recreate it under Secrets, or "
            "reconnect the integration"
        ) from None
    if not credential:
        raise EmailCredentialError(
            f"the {integration.display_name} integration has no API key — "
            "paste one on its settings page"
        )
    return require_email_provider(integration.provider), credential


async def resolve_batch_credential(
    conn: asyncpg.Connection | asyncpg.Pool,
    tenant: Tenant,
    integration_id: UUID | None,
) -> tuple[EmailProvider, str, Integration]:
    """Everything a send needs from the batch's `integration_id`.

    A null id means the integration was deleted after the batch was created —
    the FK is ``ON DELETE SET NULL`` precisely so that deleting an integration
    does not start failing, and this is where a live batch finds out.
    """
    if integration_id is None:
        raise EmailCredentialError(
            "this batch's email account has been disconnected — reconnect it, or create a "
            "new batch pointed at a different one"
        )
    integration = await load_email_integration(conn, tenant.id, integration_id)
    if integration is None:
        raise EmailCredentialError("this batch's email account no longer exists")
    provider, credential = await resolve_email_credential(tenant, integration)
    return provider, credential, integration
