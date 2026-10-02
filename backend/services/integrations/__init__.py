"""Integrations service — curated public surface.

Import what you need from here::

    from services.integrations import Integration, IntegrationResponse, create_integration
    from services import integrations
    await integrations.create_integration(body, ctx)

OAuth internals: ``services.integrations.oauth``
Provider implementations: ``services.integrations.providers.asana`` (etc.)
Catalog: ``services.integrations.catalog``
"""

from __future__ import annotations

from .catalog import (  # noqa: F401
    IMPLEMENTED_TRIGGER_TYPES,
    INTEGRATION_LOGO_URLS,
    INTEGRATION_PROVIDER_LABELS,
    INTEGRATION_PROVIDERS,
    MANUAL_PROVIDER_SET,
    NATIVE_TOOL_PROVIDER_SET,
    OAUTH_PROVIDER_SET,
    PROVIDERS,
    TOOL_PROVIDER_SET,
    TRIGGER_PROVIDER_TYPES,
    HostedMcp,
    ProviderSpec,
    TriggerSpec,
    agent_channel_for_trigger,
    auth_type_for_provider,
    default_capabilities_for_provider,
    get_provider,
    hosted_mcp,
    require_provider,
    trigger_spec,
)
from .definitions import (  # noqa: F401
    integration_config_errors,
    provider_supports_trigger,
    validate_integration_definition,
)
from .models import (  # noqa: F401
    INTEGRATION_COLUMNS,
    INTEGRATION_TRIGGER_COLUMNS,
    CreateIntegrationRequest,
    CreateIntegrationTriggerRequest,
    Integration,
    IntegrationCapabilities,
    IntegrationCatalogItem,
    IntegrationMcpTool,
    IntegrationMcpToolsResponse,
    IntegrationProvider,
    IntegrationResponse,
    IntegrationSetupField,
    IntegrationTrigger,
    IntegrationTriggerCapability,
    IntegrationTriggerResponse,
    IntegrationWebhookSetup,
    PatchIntegrationRequest,
    PatchIntegrationTriggerRequest,
    ReplyMode,
    TriggerStatus,
    TriggerType,
)
from .oauth.credentials import (  # noqa: F401
    OAuthAccessToken,
    OAuthCredential,
    get_oauth_access_token,
    load_oauth_credential,
    mark_integration_needs_reconnect,
    revoke_provider_tokens,
)
from .oauth.protocol import OAuthCredentialError  # noqa: F401
from .oauth.registry import oauth_provider_configured, require_oauth_flow  # noqa: F401
from .service import (  # noqa: F401
    capabilities_for_provider,
    create_integration,
    delete_integration,
    get_integration,
    integration_catalog,
    integration_from_row,
    list_integrations,
    list_twilio_whatsapp_senders,
    load_active_mcp_integration,
    oauth_integration_label,
    patch_integration,
    validate_definition,
)
from .triggers import (  # noqa: F401
    create_integration_trigger,
    delete_integration_trigger,
    get_active_trigger,
    list_integration_triggers,
    patch_integration_trigger,
)
from .types import (  # noqa: F401
    AgentChannel,
    IntegrationAuthType,
    IntegrationStatus,
)
