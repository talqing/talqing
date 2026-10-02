"""Integration domain structs and service request/response shapes.

``Integration`` / ``IntegrationTrigger`` are internal domain models (DB rows).
``IntegrationResponse`` / ``IntegrationTriggerResponse`` are public API
projections. Prefer the domain models everywhere inside the backend; build
response models only at HTTP / CoPilot boundaries.

Pure helpers live next to their call sites:
- OAuth finalize / start-complete: ``services.integrations.oauth.session``
- Provider defs / validation: ``services.integrations.definitions`` + ``catalog``
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from services.tools import ToolsNamespace

from .types import AgentChannel, IntegrationAuthType, IntegrationStatus

IntegrationProvider = Literal[
    "custom_mcp",
    "google_calendar",
    "cal_com",
    "calendly",
    "asana",
    "jira",
    "hubspot",
    "exa",
    "tavily",
    "resend",
    "rocketreach",
    "telegram",
    "whatsapp",
]
IntegrationName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
]
McpToolName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
# An approved tool list is never empty: approving nothing is what disabling the
# integration means, and an empty allowlist reads as "no filter" to the MCP
# client. Omit the field (or leave it null) to expose every tool the server lists.
AllowedTools = Annotated[list[McpToolName], Field(min_length=1)]
# At-rest value is always a short ``{{secrets.NAME}}`` ref.
SecretRef = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
# Create/patch accept either a secret ref or plaintext (materialized server-side).
SecretInput = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=16384)
]

TriggerStatus = Literal["active", "disabled", "needs_setup", "error"]
TriggerType = Literal[
    "telegram.message.inbound", "whatsapp.message.inbound", "whatsapp.call.inbound"
]
# public_reply = customer-visible send; internal_note = provider private note when
# supported; none = generate only (never send).
ReplyMode = Literal["public_reply", "internal_note", "none"]

# SQL column list for SELECT … FROM integrations. Matches ``Integration.from_row``.
INTEGRATION_COLUMNS = """
id, tenant_id, display_name, provider, credentials_ref,
provider_account_info, mcp_config, webhook_config, allowed_tools,
tools_namespace, metadata, status, last_webhook_received_at,
created_at, updated_at
"""

# SQL column list for SELECT … FROM integration_triggers.
INTEGRATION_TRIGGER_COLUMNS = """
id, tenant_id, integration_id, trigger_type, agent_id, enabled, status,
reply_mode, provider_subscription_ref, metadata, created_at, updated_at
"""


class IntegrationTriggerCapability(BaseModel):
    """Catalog metadata for one trigger type (owned by the backend registry)."""

    trigger_type: TriggerType
    title: str
    description: str
    agent_channel: AgentChannel


class IntegrationCapabilities(BaseModel):
    """Complete capabilities projection — always the same shape for every provider.

    Derived from ``ProviderSpec`` at response time; never stored in the DB.
    """

    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    adds_agent_tools: bool = False
    trigger_types: list[TriggerType] = Field(default_factory=list)
    triggers: list[IntegrationTriggerCapability] = Field(default_factory=list)
    # True when agents may attach this integration for MCP tools.
    attachable_as_mcp: bool = False


class CreateIntegrationRequest(BaseModel):
    display_name: IntegrationName
    provider: IntegrationProvider
    # auth_type is fixed per provider (derived from ProviderSpec); not client-set.
    # Plaintext tokens are accepted and stored as secrets server-side.
    credentials_ref: SecretInput | None = None
    provider_account_info: dict[str, Any] = Field(default_factory=dict)
    mcp_config: dict[str, Any] = Field(default_factory=dict)
    # Secret_ref fields (secret_token, …) accept plaintext or {{secrets.NAME}}.
    webhook_config: dict[str, Any] = Field(default_factory=dict)
    # Which MCP tools attached agents may call. Null exposes every tool the
    # server lists — the dashboard creates the integration, reads the tool list
    # off the live server, and patches the approved names back.
    allowed_tools: AllowedTools | None = None
    # The prefix this server's tools are presented to the model under. Omit it
    # and a hosted provider gets its own key (`notion_search`), while a
    # `custom_mcp` server gets no prefix — there is no name to derive one from.
    tools_namespace: ToolsNamespace | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PatchIntegrationRequest(BaseModel):
    display_name: IntegrationName | None = None
    # Omit / leave unset to keep the current credential. Plaintext rotates via a new secret.
    credentials_ref: SecretInput | None = None
    provider_account_info: dict[str, Any] | None = None
    mcp_config: dict[str, Any] | None = None
    webhook_config: dict[str, Any] | None = None
    # Replaces the approved list wholesale. Omitted keeps the current one; there
    # is no value that means "go back to every tool" — send the full list instead.
    allowed_tools: AllowedTools | None = None
    # Omitted keeps the current prefix; `""` clears it, and the server's own
    # tool names are presented unchanged. Those are different requests, which is
    # exactly what a reader skimming past this field will assume they are not.
    tools_namespace: ToolsNamespace | None = None
    metadata: dict[str, Any] | None = None
    status: IntegrationStatus | None = None


class IntegrationWebhookSetup(BaseModel):
    """Where the provider delivers, and whether anything has arrived yet.

    Talqing registers this URL with the provider when a trigger is enabled. A
    Gupshup WhatsApp integration's URL carries its token.
    """

    callback_url: str
    delivery_status: Literal["waiting", "receiving"]
    last_received_at: datetime | None = None


class Integration(BaseModel):
    """Internal domain model for a persisted integration row.

    Use this for channel adapters, triggers, delivery, webhooks, and service
    logic. Public APIs return ``IntegrationResponse`` via ``to_response()``.
    """

    id: UUID
    tenant_id: UUID
    display_name: str
    provider: IntegrationProvider
    # Derived from ProviderSpec — connect lifecycle (oauth | manual), not stored.
    auth_type: IntegrationAuthType
    credentials_ref: str | None = None
    provider_account_info: dict[str, Any] = Field(default_factory=dict)
    mcp_config: dict[str, Any] = Field(default_factory=dict)
    webhook_config: dict[str, Any] = Field(default_factory=dict)
    # None = every tool the MCP server lists (nothing approved yet). A list is
    # the exact set attached agents may call; the compiler filters to it.
    allowed_tools: list[str] | None = None
    # `<tools_namespace>_<tool>` is what the model is shown. Blank presents the
    # server's own names; `services.tools.exposed_tool_name` is the one rule.
    tools_namespace: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    status: IntegrationStatus
    last_webhook_received_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @staticmethod
    def _json_object(row: Mapping[str, Any], key: str) -> dict[str, Any]:
        value = row[key]
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise TypeError(f"integration.{key} must be a JSON object")
        return dict(value)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Integration:
        """Build an Integration from an asyncpg Record or mapping of DB columns."""
        # Local import: catalog pulls provider modules; keep models load light.
        from .catalog import auth_type_for_provider

        provider = str(row["provider"])
        return cls(
            id=row["id"],
            tenant_id=row["tenant_id"],
            display_name=row["display_name"],
            provider=provider,  # type: ignore[arg-type]
            auth_type=auth_type_for_provider(provider),
            credentials_ref=row["credentials_ref"],
            provider_account_info=cls._json_object(row, "provider_account_info"),
            mcp_config=cls._json_object(row, "mcp_config"),
            webhook_config=cls._json_object(row, "webhook_config"),
            allowed_tools=(
                list(row["allowed_tools"]) if row["allowed_tools"] is not None else None
            ),
            tools_namespace=row["tools_namespace"],
            metadata=cls._json_object(row, "metadata"),
            status=row["status"],
            last_webhook_received_at=row.get("last_webhook_received_at"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _capabilities(self) -> dict[str, Any]:
        from .catalog import default_capabilities_for_provider

        capabilities = default_capabilities_for_provider(self.provider)
        # WhatsApp calls come through Twilio only, so a Gupshup number offers none.
        if self.provider == "whatsapp" and self.provider_account_info.get("bsp") != "twilio":
            call = "whatsapp.call.inbound"
            capabilities["trigger_types"] = [t for t in capabilities["trigger_types"] if t != call]
            capabilities["triggers"] = [
                t for t in capabilities["triggers"] if t["trigger_type"] != call
            ]
        return capabilities

    def to_response(self) -> IntegrationResponse:
        """Public API projection (labels, capabilities, webhook setup)."""
        from settings import get_settings

        from .catalog import (
            INTEGRATION_LOGO_URLS,
            INTEGRATION_PROVIDER_LABELS,
            TRIGGER_PROVIDER_TYPES,
        )

        webhook_setup: IntegrationWebhookSetup | None = None
        api_base = get_settings().app.api_public_url.rstrip("/")
        # A provider has a callback URL because it has a trigger: enabling one
        # is what registers the webhook, and nothing else here receives.
        if self.provider in TRIGGER_PROVIDER_TYPES:
            callback_url = (
                f"{api_base}/v1/integrations/webhooks/{self.provider}/{self.tenant_id}/{self.id}"
            )
            if self.provider == "whatsapp" and self.provider_account_info.get("bsp") == "gupshup":
                from .providers.whatsapp import gupshup_callback_url

                callback_url = gupshup_callback_url(self.tenant_id, self.id)
            webhook_setup = IntegrationWebhookSetup(
                callback_url=callback_url,
                delivery_status="receiving" if self.last_webhook_received_at else "waiting",
                last_received_at=self.last_webhook_received_at,
            )

        return IntegrationResponse(
            id=self.id,
            display_name=self.display_name,
            provider=self.provider,
            provider_label=INTEGRATION_PROVIDER_LABELS.get(self.provider, self.provider),
            logo_url=INTEGRATION_LOGO_URLS.get(self.provider),
            auth_type=self.auth_type,
            # Secret fields store {{secrets.NAME}} references, not plaintext —
            # return them so the dashboard can show and edit which secret is wired.
            credentials_ref=self.credentials_ref,
            provider_account_info=self.provider_account_info,
            capabilities=IntegrationCapabilities.model_validate(self._capabilities()),
            mcp_config=self.mcp_config,
            webhook_config=self.webhook_config,
            allowed_tools=self.allowed_tools,
            tools_namespace=self.tools_namespace,
            webhook_setup=webhook_setup,
            metadata=self.metadata,
            status=self.status,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


class IntegrationResponse(BaseModel):
    """Public API representation of an integration (HTTP + CoPilot)."""

    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    id: UUID
    display_name: str
    provider: IntegrationProvider
    provider_label: str
    logo_url: str | None = None
    # Derived from ProviderSpec — connect lifecycle (oauth | manual), not stored.
    auth_type: IntegrationAuthType
    credentials_ref: str | None = None
    provider_account_info: dict[str, Any]
    # Always derived from provider at response time — never a DB column.
    capabilities: IntegrationCapabilities
    mcp_config: dict[str, Any]
    webhook_config: dict[str, Any]
    # Approved MCP tools. Null means every tool the server lists is exposed.
    allowed_tools: list[str] | None = None
    # Read it back before writing a prompt: `<tools_namespace>_<tool>` is the
    # name the model knows a tool by. Blank means the server's own names.
    tools_namespace: str = ""
    webhook_setup: IntegrationWebhookSetup | None = None
    metadata: dict[str, Any]
    status: IntegrationStatus
    created_at: datetime
    updated_at: datetime


class IntegrationSetupField(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    key: str
    label: str
    type: Literal["text", "secret_ref", "string_list", "headers"]
    target: Literal[
        "credentials_ref",
        "provider_account_info",
        "mcp_config",
        "webhook_config",
        "metadata",
    ] = "mcp_config"
    required: bool = False
    placeholder: str | None = None
    hint: str | None = None


class IntegrationCatalogItem(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    provider: IntegrationProvider
    name: str
    description: str
    category: str
    # Connect path for this provider (oauth → Connect button; manual → form).
    auth_type: IntegrationAuthType
    logo_url: str | None = None
    enabled: bool = True
    capabilities: IntegrationCapabilities = Field(default_factory=IntegrationCapabilities)
    setup_fields: list[IntegrationSetupField] = Field(default_factory=list)


class IntegrationMcpTool(BaseModel):
    # `name` is the server's own, which is what `allowed_tools` approves;
    # `exposed_name` is what the model is shown, and what a prompt must say.
    name: str
    exposed_name: str
    description: str = ""


class IntegrationMcpToolsResponse(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    tools: list[IntegrationMcpTool]


class IntegrationTrigger(BaseModel):
    """Internal domain model for a persisted integration trigger row.

    Use this for adapters, inbound routing, and service logic. Public APIs
    return ``IntegrationTriggerResponse`` via ``to_response()``.
    """

    id: UUID
    tenant_id: UUID
    integration_id: UUID
    trigger_type: TriggerType
    agent_id: UUID | None = None
    enabled: bool
    status: TriggerStatus
    reply_mode: ReplyMode
    provider_subscription_ref: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    @staticmethod
    def _json_object(row: Mapping[str, Any], key: str) -> dict[str, Any]:
        value = row[key]
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise TypeError(f"integration_trigger.{key} must be a JSON object")
        return dict(value)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> IntegrationTrigger:
        """Build an IntegrationTrigger from an asyncpg Record or mapping."""
        return cls(
            id=row["id"],
            tenant_id=row["tenant_id"],
            integration_id=row["integration_id"],
            trigger_type=row["trigger_type"],
            agent_id=row["agent_id"],
            enabled=bool(row["enabled"]),
            status=row["status"],
            reply_mode=row["reply_mode"],
            provider_subscription_ref=row["provider_subscription_ref"],
            metadata=cls._json_object(row, "metadata"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def to_response(self) -> IntegrationTriggerResponse:
        """Public API projection (no tenant_id)."""
        return IntegrationTriggerResponse(
            id=self.id,
            integration_id=self.integration_id,
            trigger_type=self.trigger_type,
            agent_id=self.agent_id,
            enabled=self.enabled,
            status=self.status,
            reply_mode=self.reply_mode,
            provider_subscription_ref=self.provider_subscription_ref,
            metadata=self.metadata,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


class IntegrationTriggerResponse(BaseModel):
    """Public API representation of an integration trigger (HTTP + CoPilot)."""

    id: UUID
    integration_id: UUID
    trigger_type: TriggerType
    agent_id: UUID | None = None
    enabled: bool
    status: TriggerStatus
    reply_mode: ReplyMode
    provider_subscription_ref: str | None = None
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class CreateIntegrationTriggerRequest(BaseModel):
    trigger_type: TriggerType
    agent_id: UUID | None = None
    enabled: bool = False
    reply_mode: ReplyMode = "public_reply"
    provider_subscription_ref: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PatchIntegrationTriggerRequest(BaseModel):
    trigger_type: TriggerType | None = None
    agent_id: UUID | None = None
    enabled: bool | None = None
    status: TriggerStatus | None = None
    reply_mode: ReplyMode | None = None
    provider_subscription_ref: str | None = None
    metadata: dict[str, Any] | None = None
