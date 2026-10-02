"""Integration provider catalog — product metadata and derived sets.

Implementations live under ``providers/`` (``asana.py``, …). Register each
provider here (metadata + MCP + catalog fields). OAuth connect/refresh/revoke
live on each provider module's ``*OAuthFlow`` class. Config validation lives
in ``definitions``.

## Credential placement

- OAuth tokens → ``integration_oauth_credentials``
- Primary non-OAuth account secret → ``credentials_ref`` (bot token,
  Exa/Tavily/Resend API key) — at rest always ``{{secrets.NAME}}``; create/patch
  accept plaintext and materialize via secrets service
- Inbound webhook secrets → ``webhook_config`` secret refs (same materialize rules)
- custom MCP url/headers → ``mcp_config`` (headers still use hand-wired
  ``{{secrets.NAME}}``; not auto-materialized)

## ``provider_account_info`` (identity bag)

JSONB on ``integrations`` for the connected external account. Not credentials.

**Uniqueness keys** (partial unique indexes — must be stable routing ids):

| Provider family | Key | Index |
|---|---|---|
| Telegram | ``bot_id`` | ``uq_integrations_telegram_bot`` |
| WhatsApp | ``sender_e164`` | ``uq_integrations_whatsapp_sender`` |
| OAuth MCP | ``provider_subject`` (HubSpot: portal hub_id) | ``uq_integrations_oauth_subject`` |

**Display / UX fields** (not uniqueness keys): ``account_email``,
``account_name``, ``account_username``, ``bot_username``, ``connected_at``.

Runtime routing and thread keys must use the uniqueness keys, not display fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .providers.asana import ASANA_MCP_URL
from .providers.cal_com import CAL_COM_MCP_URL
from .providers.calendly import CALENDLY_MCP_URL
from .providers.exa import EXA_MCP_URL
from .providers.hubspot import HUBSPOT_MCP_URL
from .providers.jira import JIRA_MCP_URL
from .providers.resend import RESEND_MCP_URL
from .providers.rocketreach import ROCKETREACH_MCP_URL
from .providers.tavily import TAVILY_MCP_URL
from .types import AgentChannel, IntegrationAuthType

# Form control types (orthogonal to IntegrationAuthType).
SetupFieldType = Literal["text", "secret_ref", "string_list", "headers"]
SetupFieldTarget = Literal[
    "credentials_ref",
    "provider_account_info",
    "mcp_config",
    "webhook_config",
    "metadata",
]


@dataclass(frozen=True, slots=True)
class SetupField:
    key: str
    label: str
    type: SetupFieldType
    target: SetupFieldTarget = "mcp_config"
    required: bool = False
    placeholder: str | None = None
    hint: str | None = None
    # Preferred secrets-table name when the user pastes plaintext (not exposed
    # on the public catalog API). On name collision the backend appends _<hex>.
    default_secret_name: str | None = None


@dataclass(frozen=True, slots=True)
class TriggerSpec:
    """Catalog metadata for one provider event → agent trigger."""

    trigger_type: str
    title: str
    description: str
    agent_channel: AgentChannel


@dataclass(frozen=True, slots=True)
class HostedMcp:
    """Location of a vendor-hosted MCP server (URL + transport only).

    How credentials are applied is provider-specific code in
    ``compiler.integrations`` — not an auth_mode enum on this struct.
    """

    url: str
    transport_type: str = "streamable_http"


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """Product + runtime definition of one integration provider."""

    provider: str
    label: str
    description: str
    category: str
    auth_type: IntegrationAuthType
    logo_url: str | None = None
    # Derived capabilities (never stored in DB).
    adds_agent_tools: bool = False
    triggers: tuple[TriggerSpec, ...] = ()
    hosted_mcp: HostedMcp | None = None
    # Tools we implement ourselves against the provider's own API, rather than
    # proxying a server the vendor hosts (`compiler.native`). A sibling of
    # `hosted_mcp` rather than a magic value on it: they are two different
    # answers to "where do this provider's tools come from", and exactly one of
    # them is set on a tool-bearing provider.
    native_tools: bool = False
    setup_fields: tuple[SetupField, ...] = field(default_factory=tuple)

    @property
    def trigger_types(self) -> tuple[str, ...]:
        return tuple(t.trigger_type for t in self.triggers)

    def capabilities(self) -> dict[str, Any]:
        """Complete client-facing capabilities (fixed shape for every provider)."""
        trigger_payloads = [
            {
                "trigger_type": t.trigger_type,
                "title": t.title,
                "description": t.description,
                "agent_channel": t.agent_channel,
            }
            for t in self.triggers
        ]
        return {
            "adds_agent_tools": self.adds_agent_tools,
            "trigger_types": list(self.trigger_types),
            "triggers": trigger_payloads,
            # Agents may attach this integration for MCP tools (not channel deploy).
            "attachable_as_mcp": self.adds_agent_tools,
        }


# ── Logo URLs ──────────────────────────────────────────────────────────────

_LOGOS = {
    "google_calendar": "https://calendar.google.com/googlecalendar/images/favicons_2020q4/calendar_31.ico",
    "cal_com": "https://cal.com/favicon.ico",
    "calendly": "https://calendly.com/favicon.ico",
    "asana": "https://asana.com/favicon.ico",
    "jira": "https://jira.atlassian.com/favicon.ico",
    "hubspot": "https://www.hubspot.com/favicon.ico",
    "exa": "https://exa.ai/favicon.ico",
    "rocketreach": "https://rocketreach.co/favicon.ico",
    "tavily": "https://www.tavily.com/favicon.ico",
    # resend.com/favicon.ico is a 404 that returns the marketing HTML page.
    "resend": "https://resend.com/static/favicons/favicon.ico",
    "telegram": "https://telegram.org/favicon.ico",
    # whatsapp.com refuses cross-origin image loads, so the brand mark comes
    # from Wikimedia Commons instead.
    "whatsapp": "https://upload.wikimedia.org/wikipedia/commons/6/6b/WhatsApp.svg",
}


PROVIDERS: dict[str, ProviderSpec] = {
    "custom_mcp": ProviderSpec(
        provider="custom_mcp",
        label="Custom MCP Server",
        description=(
            "Connect your own remote MCP server. All tools the server exposes "
            "are available to attached agents."
        ),
        category="Developer Tools",
        auth_type="manual",
        logo_url=_LOGOS.get("custom_mcp"),
        adds_agent_tools=True,
        setup_fields=(
            SetupField(
                key="url",
                label="MCP server URL",
                type="text",
                target="mcp_config",
                required=True,
                placeholder="https://mcp.example.com/api/mcp",
            ),
            SetupField(
                key="headers",
                label="Headers",
                type="headers",
                target="mcp_config",
                hint="Use secrets like {{secrets.MCP_TOKEN}} for credentials.",
            ),
        ),
    ),
    "exa": ProviderSpec(
        provider="exa",
        label="Exa",
        description="Use Exa's hosted MCP server for web search.",
        category="Search",
        auth_type="manual",
        logo_url=_LOGOS.get("exa"),
        adds_agent_tools=True,
        hosted_mcp=HostedMcp(url=EXA_MCP_URL),
        setup_fields=(
            SetupField(
                key="credentials_ref",
                label="Exa API key",
                type="secret_ref",
                target="credentials_ref",
                required=True,
                placeholder="exa-…",
                hint=(
                    "Create a key at dashboard.exa.ai/api-keys. Exa's keyless tier "
                    "is a shared pool whose rate limit strangers can exhaust, so an "
                    "agent on it fails mid-call. Stored as an encrypted secret."
                ),
                default_secret_name="EXA_API_KEY",
            ),
        ),
    ),
    "tavily": ProviderSpec(
        provider="tavily",
        label="Tavily",
        description="Use Tavily's hosted MCP server for web search and extraction.",
        category="Search",
        auth_type="manual",
        logo_url=_LOGOS.get("tavily"),
        adds_agent_tools=True,
        hosted_mcp=HostedMcp(url=TAVILY_MCP_URL),
        setup_fields=(
            SetupField(
                key="credentials_ref",
                label="Tavily API key",
                type="secret_ref",
                target="credentials_ref",
                required=True,
                placeholder="tvly-…",
                hint=(
                    "Create a key at app.tavily.com. Tavily's MCP server has no "
                    "keyless mode — it answers every unauthenticated request "
                    "with a 401. Stored as an encrypted secret."
                ),
                default_secret_name="TAVILY_API_KEY",
            ),
        ),
    ),
    "resend": ProviderSpec(
        provider="resend",
        label="Resend",
        description="Use Resend's hosted MCP server to send and manage email.",
        category="Email",
        auth_type="manual",
        logo_url=_LOGOS.get("resend"),
        adds_agent_tools=True,
        hosted_mcp=HostedMcp(url=RESEND_MCP_URL),
        setup_fields=(
            SetupField(
                key="credentials_ref",
                label="Resend API key",
                type="secret_ref",
                target="credentials_ref",
                required=True,
                placeholder="re_…",
                hint=(
                    "Create a key at resend.com/api-keys. Resend's MCP server has "
                    "no keyless mode — the key is what scopes the tools to your "
                    "account. Stored as an encrypted secret."
                ),
                default_secret_name="RESEND_API_KEY",
            ),
        ),
    ),
    "google_calendar": ProviderSpec(
        provider="google_calendar",
        label="Google Calendar",
        description=(
            "Read, create, change and cancel events, and find free time across "
            "attendees. Events can only be written to calendars the connected "
            "account owns, not ones merely shared with it."
        ),
        category="Scheduling",
        auth_type="oauth",
        logo_url=_LOGOS.get("google_calendar"),
        adds_agent_tools=True,
        native_tools=True,
    ),
    "cal_com": ProviderSpec(
        provider="cal_com",
        label="Cal.com",
        description="Use Cal.com's hosted MCP server for scheduling.",
        category="Scheduling",
        auth_type="oauth",
        logo_url=_LOGOS.get("cal_com"),
        adds_agent_tools=True,
        hosted_mcp=HostedMcp(url=CAL_COM_MCP_URL),
    ),
    "calendly": ProviderSpec(
        provider="calendly",
        label="Calendly",
        description="Use Calendly's hosted MCP server for scheduling.",
        category="Scheduling",
        auth_type="oauth",
        logo_url=_LOGOS.get("calendly"),
        adds_agent_tools=True,
        hosted_mcp=HostedMcp(url=CALENDLY_MCP_URL),
    ),
    "asana": ProviderSpec(
        provider="asana",
        label="Asana",
        description="Use Asana's hosted MCP server for work management.",
        category="Project Management",
        auth_type="oauth",
        logo_url=_LOGOS.get("asana"),
        adds_agent_tools=True,
        hosted_mcp=HostedMcp(url=ASANA_MCP_URL),
    ),
    "jira": ProviderSpec(
        provider="jira",
        label="Jira",
        description="Use Atlassian's hosted MCP server for Jira issues.",
        category="Project Management",
        auth_type="oauth",
        logo_url=_LOGOS.get("jira"),
        adds_agent_tools=True,
        hosted_mcp=HostedMcp(url=JIRA_MCP_URL),
    ),
    "hubspot": ProviderSpec(
        provider="hubspot",
        label="HubSpot",
        description="Use HubSpot's hosted MCP server for CRM data.",
        category="CRM",
        auth_type="oauth",
        logo_url=_LOGOS.get("hubspot"),
        adds_agent_tools=True,
        hosted_mcp=HostedMcp(url=HUBSPOT_MCP_URL),
    ),
    "rocketreach": ProviderSpec(
        provider="rocketreach",
        label="RocketReach",
        description=(
            "Use RocketReach's hosted MCP server to find a person's work email, "
            "phone number and profile from a name, a company or a domain. "
            "Searching is free; each lookup spends export credits from your "
            "RocketReach plan - an agent task running one per row of a 5 000-row "
            "list spends 5 000 of them, and nothing here caps that."
        ),
        category="Enrichment",
        auth_type="oauth",
        logo_url=_LOGOS.get("rocketreach"),
        adds_agent_tools=True,
        hosted_mcp=HostedMcp(url=ROCKETREACH_MCP_URL),
    ),
    "telegram": ProviderSpec(
        provider="telegram",
        label="Telegram",
        description="Route private Telegram bot messages to a published text agent.",
        category="Messaging",
        auth_type="manual",
        logo_url=_LOGOS.get("telegram"),
        triggers=(
            TriggerSpec(
                trigger_type="telegram.message.inbound",
                title="Inbound Telegram messages",
                description=(
                    "Enable to register the bot webhook automatically and reply "
                    "in the same private chat. You do not need to paste a URL "
                    "into BotFather."
                ),
                agent_channel="text",
            ),
        ),
        setup_fields=(
            SetupField(
                key="credentials_ref",
                label="Bot token",
                type="secret_ref",
                target="credentials_ref",
                required=True,
                placeholder="123456789:AAH…",
                hint=(
                    "Paste the token from BotFather. Talqing stores it as an "
                    "encrypted secret, calls getMe, and saves the bot id/username."
                ),
                default_secret_name="TELEGRAM_BOT_TOKEN",
            ),
            SetupField(
                key="secret_token",
                label="Webhook secret token",
                type="secret_ref",
                target="webhook_config",
                required=True,
                placeholder="random-secret-token",
                hint=(
                    "Any string Telegram will send in "
                    "X-Telegram-Bot-Api-Secret-Token (1–256 chars: letters, "
                    "digits, underscore, or hyphen). Stored as an encrypted secret."
                ),
                default_secret_name="TELEGRAM_WEBHOOK_SECRET",
            ),
        ),
    ),
    "whatsapp": ProviderSpec(
        provider="whatsapp",
        label="WhatsApp",
        description=(
            "Put a published text agent on a WhatsApp number you have at Twilio or "
            "Gupshup, answer its calls with a voice agent (Twilio), and send approved "
            "templates to a list."
        ),
        category="Messaging",
        auth_type="manual",
        logo_url=_LOGOS.get("whatsapp"),
        triggers=(
            TriggerSpec(
                trigger_type="whatsapp.message.inbound",
                title="Inbound WhatsApp messages",
                description="The agent answers every text sent to this number.",
                agent_channel="text",
            ),
            TriggerSpec(
                trigger_type="whatsapp.call.inbound",
                title="Inbound WhatsApp calls",
                description="Shows the call button in WhatsApp. Numbers on Twilio only.",
                agent_channel="voice",
            ),
        ),
        setup_fields=(
            SetupField(
                key="bsp",
                label="Provider",
                type="text",
                target="provider_account_info",
                required=True,
                placeholder="twilio",
                hint="`twilio` or `gupshup` — where the number is registered.",
            ),
            SetupField(
                key="sender_e164",
                label="WhatsApp number",
                type="text",
                target="provider_account_info",
                required=True,
                placeholder="+14155550100",
            ),
            SetupField(
                key="account_sid",
                label="Account SID",
                type="text",
                target="provider_account_info",
                placeholder="AC…",
                hint="Twilio only.",
            ),
            SetupField(
                key="app_id",
                label="App ID",
                type="text",
                target="provider_account_info",
                hint="Gupshup only: the app's id on apps.gupshup.io.",
            ),
            SetupField(
                key="app_name",
                label="App name",
                type="text",
                target="provider_account_info",
                hint="Gupshup only: the app's name, exactly as Gupshup shows it.",
            ),
            SetupField(
                key="credentials_ref",
                label="Auth token or API key",
                type="secret_ref",
                target="credentials_ref",
                required=True,
                hint="Twilio: the account's Auth Token. Gupshup: the app's API key.",
                default_secret_name="WHATSAPP_CREDENTIAL",
            ),
        ),
    ),
}


# Derived sets — always recompute from PROVIDERS so they cannot drift.
INTEGRATION_PROVIDERS = frozenset(PROVIDERS)
TOOL_PROVIDER_SET = frozenset(p for p, s in PROVIDERS.items() if s.adds_agent_tools)
NATIVE_TOOL_PROVIDER_SET = frozenset(p for p, s in PROVIDERS.items() if s.native_tools)
OAUTH_PROVIDER_SET = frozenset(p for p, s in PROVIDERS.items() if s.auth_type == "oauth")
MANUAL_PROVIDER_SET = frozenset(p for p, s in PROVIDERS.items() if s.auth_type == "manual")
TRIGGER_PROVIDER_TYPES: dict[str, frozenset[str]] = {
    p: frozenset(s.trigger_types) for p, s in PROVIDERS.items() if s.triggers
}
# All trigger types registered in the catalog (implemented when listed here).
IMPLEMENTED_TRIGGER_TYPES: frozenset[str] = frozenset(
    t.trigger_type for s in PROVIDERS.values() for t in s.triggers
)
INTEGRATION_PROVIDER_LABELS = {p: s.label for p, s in PROVIDERS.items()}
INTEGRATION_LOGO_URLS = {p: s.logo_url for p, s in PROVIDERS.items() if s.logo_url}


def get_provider(provider: str) -> ProviderSpec | None:
    return PROVIDERS.get(provider)


def require_provider(provider: str) -> ProviderSpec:
    spec = PROVIDERS.get(provider)
    if spec is None:
        raise KeyError(f"unknown integration provider: {provider}")
    return spec


def auth_type_for_provider(provider: str) -> IntegrationAuthType:
    return require_provider(provider).auth_type


def default_capabilities_for_provider(provider: str) -> dict[str, Any]:
    return require_provider(provider).capabilities()


def hosted_mcp(provider: str) -> HostedMcp | None:
    spec = PROVIDERS.get(provider)
    return spec.hosted_mcp if spec else None


def provider_supports_trigger(provider: str, trigger_type: str) -> bool:
    return trigger_type in TRIGGER_PROVIDER_TYPES.get(provider, frozenset())


def trigger_spec(trigger_type: str) -> TriggerSpec | None:
    """Look up catalog metadata for a trigger type, if registered."""
    for spec in PROVIDERS.values():
        for trigger in spec.triggers:
            if trigger.trigger_type == trigger_type:
                return trigger
    return None


def agent_channel_for_trigger(trigger_type: str) -> AgentChannel | None:
    spec = trigger_spec(trigger_type)
    return spec.agent_channel if spec else None
