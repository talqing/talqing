"""Email provider implementations, and the lookup from an integration to one.

One entry per provider that can send a batch's email. An integration provider
that is not here can still be attached to an agent as an MCP server — the two
are unrelated uses of one credential.
"""

from __future__ import annotations

from ..provider import EmailProvider
from .resend import ResendEmailProvider, resend_provider

_PROVIDERS: dict[str, EmailProvider] = {resend_provider.provider: resend_provider}

# What `create_email_batch` will accept as an integration, and what the frontend
# offers in its account picker.
EMAIL_PROVIDERS = frozenset(_PROVIDERS)


def get_email_provider(provider: str) -> EmailProvider | None:
    return _PROVIDERS.get(provider)


def require_email_provider(provider: str) -> EmailProvider:
    sender = _PROVIDERS.get(provider)
    if sender is None:
        raise RuntimeError(f"no email provider for {provider!r}")
    return sender


__all__ = [
    "EMAIL_PROVIDERS",
    "ResendEmailProvider",
    "get_email_provider",
    "require_email_provider",
    "resend_provider",
]
