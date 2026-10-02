"""Shared integration literals — layer 0, no package-internal imports.

Catalog (``providers``) and API shapes (``models``) both depend on these.
"""

from __future__ import annotations

from typing import Literal

# Connect/credential lifecycle — not MCP transport, not field-level secret refs.
# oauth  → OAuth install; tokens in integration_oauth_credentials
# manual → setup_fields form; secrets live in the secrets store via {{secrets.NAME}}
IntegrationAuthType = Literal["oauth", "manual"]
IntegrationStatus = Literal["active", "disabled", "needs_reconnect", "error"]
AgentChannel = Literal["text", "voice"]
