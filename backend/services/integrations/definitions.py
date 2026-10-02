"""Integration config validation.

Provider catalog / auth types / capabilities live in ``catalog``.
This module only validates structured fields on create/patch.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from services.tools import template_refs, unknown_template_tokens

from .catalog import (
    INTEGRATION_PROVIDERS,
    OAUTH_PROVIDER_SET,
)
from .catalog import (
    provider_supports_trigger as _provider_supports_trigger,
)
from .models import Integration

SECRET_REF_RE = re.compile(r"^\{\{\s*secrets\.([A-Za-z0-9_]+)\s*\}\}$")

# The only identity fields an OAuth provider may store on
# `provider_account_info`. Public because connect enforces it too
# (`oauth.session`): patch validates this bag and connect writes it straight to
# the DB, so a provider emitting anything else connects cleanly and then fails
# every later edit.
OAUTH_ACCOUNT_KEYS = {
    "account_email",
    "account_username",
    "account_name",
    "account_id",
    "provider_subject",
    "connected_at",
}


# Every key a WhatsApp integration stores, per BSP; all of them are required.
# `sender_sid` is Twilio's id for the sender, read off the account at connect.
WHATSAPP_ACCOUNT_KEYS = {
    "twilio": ("bsp", "account_sid", "sender_e164", "sender_sid"),
    "gupshup": ("bsp", "app_id", "app_name", "sender_e164"),
}


def _unknown_config_fields(provider: str, cfg: Mapping[str, Any], allowed: set[str]) -> list[str]:
    return [f"{provider}: unknown config field '{key}'" for key in cfg if key not in allowed]


def _required_string(
    provider: str, cfg: Mapping[str, Any], key: str, label: str | None = None
) -> list[str]:
    value = cfg.get(key)
    name = label or key
    if not isinstance(value, str) or not value.strip():
        return [f"{provider}: {name} is required"]
    return []


def _optional_string(provider: str, cfg: Mapping[str, Any], key: str) -> list[str]:
    if key not in cfg or cfg.get(key) is None:
        return []
    if not isinstance(cfg.get(key), str):
        return [f"{provider}: {key} must be a string"]
    return []


def _optional_string_map(provider: str, cfg: Mapping[str, Any], key: str) -> list[str]:
    if key not in cfg or cfg.get(key) is None:
        return []
    value = cfg.get(key)
    if not isinstance(value, Mapping):
        return [f"{provider}: {key} must be an object"]
    for k, v in value.items():
        if not isinstance(k, str) or not isinstance(v, str):
            return [f"{provider}: {key} must be an object of string values"]
    return []


def _secret_ref_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    match = SECRET_REF_RE.fullmatch(value.strip())
    return match.group(1) if match else None


def _secret_ref_errors(
    provider: str,
    value: object,
    label: str,
    *,
    required: bool,
    secret_names: set[str] | None,
) -> list[str]:
    if value is None or value == "":
        return [f"{provider}: {label} is required"] if required else []
    if not isinstance(value, str):
        return [f"{provider}: {label} must be a secret reference like {{{{secrets.NAME}}}}"]
    name = _secret_ref_name(value)
    if name is None:
        return [
            f"{provider}: {label} must be a secret reference like {{{{secrets.NAME}}}}, not plaintext"
        ]
    if secret_names is not None and name not in secret_names:
        return [f"{provider}: secret '{name}' does not exist"]
    return []


def _object(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def integration_config_errors(
    provider: str,
    *,
    credentials_ref: object,
    provider_account_info: object,
    mcp_config: object,
    webhook_config: object,
    metadata: object,
    secret_names: set[str] | None = None,
) -> list[str]:
    if provider not in INTEGRATION_PROVIDERS:
        return [f"unknown integration provider: {provider}"]

    try:
        provider_account_info = _object(provider_account_info or {}, "provider_account_info")
        mcp = _object(mcp_config or {}, "mcp_config")
        webhook = _object(webhook_config or {}, "webhook_config")
        _object(metadata or {}, "metadata")
    except ValueError as exc:
        return [f"{provider}: {exc}"]

    errors: list[str] = []
    if credentials_ref is not None:
        errors.extend(
            _secret_ref_errors(
                provider,
                credentials_ref,
                "credentials_ref",
                required=False,
                secret_names=secret_names,
            )
        )

    if provider == "custom_mcp":
        errors.extend(_unknown_config_fields(provider, mcp, {"url", "headers"}))
        errors.extend(_required_string(provider, mcp, "url", "MCP server URL"))
        errors.extend(_optional_string_map(provider, mcp, "headers"))
        return errors

    if provider in {"exa", "tavily", "resend"}:
        # Primary API key lives on credentials_ref only; mcp_config stays empty.
        # The key is required on all three, for two different reasons. Tavily and
        # Resend refuse an unauthenticated `initialize` outright (401, measured —
        # Tavily's docs describe a keyless tier that its MCP server does not
        # have). Exa does answer keyless, on a shared anonymous pool whose rate
        # limit is reached by strangers: an agent built on it works in testing
        # and fails mid-call with "You've hit Exa's free MCP rate limit", which
        # is the unpredictable-in-production failure a required field turns into
        # a setup-time one.
        errors.extend(_unknown_config_fields(provider, mcp, set()))
        errors.extend(
            _secret_ref_errors(
                provider,
                credentials_ref,
                "credentials_ref",
                required=True,
                secret_names=secret_names,
            )
        )
        return errors

    if provider in OAUTH_PROVIDER_SET:
        # OAuth integrations do not store transport config in mcp_config.
        errors.extend(_unknown_config_fields(provider, mcp, set()))
        errors.extend(_unknown_config_fields(provider, provider_account_info, OAUTH_ACCOUNT_KEYS))
        for key in OAUTH_ACCOUNT_KEYS:
            errors.extend(_optional_string(provider, provider_account_info, key))
        return errors

    if provider == "telegram":
        errors.extend(
            _secret_ref_errors(
                provider,
                credentials_ref,
                "credentials_ref",
                required=True,
                secret_names=secret_names,
            )
        )
        errors.extend(
            _unknown_config_fields(provider, provider_account_info, {"bot_id", "bot_username"})
        )
        errors.extend(_required_string(provider, provider_account_info, "bot_id", "bot_id"))
        errors.extend(_optional_string(provider, provider_account_info, "bot_username"))
        errors.extend(_unknown_config_fields(provider, webhook, {"secret_token"}))
        errors.extend(
            _secret_ref_errors(
                provider,
                webhook.get("secret_token"),
                "secret_token",
                required=True,
                secret_names=secret_names,
            )
        )
        errors.extend(_unknown_config_fields(provider, mcp, set()))
        return errors

    if provider == "whatsapp":
        errors.extend(
            _secret_ref_errors(
                provider,
                credentials_ref,
                "credentials_ref",
                required=True,
                secret_names=secret_names,
            )
        )
        bsp = provider_account_info.get("bsp")
        if bsp not in WHATSAPP_ACCOUNT_KEYS:
            return [*errors, f"{provider}: bsp must be one of {sorted(WHATSAPP_ACCOUNT_KEYS)}"]
        keys = WHATSAPP_ACCOUNT_KEYS[bsp]
        errors.extend(_unknown_config_fields(provider, provider_account_info, set(keys)))
        for key in keys:
            errors.extend(_required_string(provider, provider_account_info, key))
        errors.extend(_unknown_config_fields(provider, webhook, set()))
        errors.extend(_unknown_config_fields(provider, mcp, set()))
        return errors

    return errors


def validate_integration_definition(
    definition: Integration | Mapping[str, Any],
    *,
    secret_names: set[str],
) -> list[str]:
    """Validate config for a persisted ``Integration`` or a pre-persist draft mapping."""
    if isinstance(definition, Integration):
        provider = definition.provider
        credentials_ref = definition.credentials_ref
        provider_account_info = definition.provider_account_info
        mcp_config = definition.mcp_config
        webhook_config = definition.webhook_config
        metadata = definition.metadata
    else:
        provider = str(definition.get("provider") or "")
        credentials_ref = definition.get("credentials_ref")
        provider_account_info = definition.get("provider_account_info") or {}
        mcp_config = definition.get("mcp_config") or {}
        webhook_config = definition.get("webhook_config") or {}
        metadata = definition.get("metadata") or {}

    errors = integration_config_errors(
        provider,
        credentials_ref=credentials_ref,
        provider_account_info=provider_account_info,
        mcp_config=mcp_config,
        webhook_config=webhook_config,
        metadata=metadata,
        secret_names=secret_names,
    )

    templated = {
        "credentials_ref": credentials_ref,
        "provider_account_info": provider_account_info,
        "mcp_config": mcp_config,
        "webhook_config": webhook_config,
        "metadata": metadata,
    }
    # A token whose root is not a template root at all — a typo, or a spelling
    # borrowed from another platform. `template_refs` is built from the known
    # roots, so it cannot see one, and the literal braces would be sent to the
    # provider as part of a credential or a webhook URL.
    for clause in unknown_template_tokens(
        templated, vocabulary="An integration can read {{secrets.…}}"
    ):
        errors.append(f"{provider}: {clause}")
    for root, key in template_refs(templated):
        if root == "secrets":
            if key not in secret_names:
                errors.append(f"{provider}: secret '{key}' does not exist")
        else:
            errors.append(f"{provider}: integrations may only reference secrets, not {root}")
    return list(dict.fromkeys(errors))


def provider_supports_trigger(
    integration: Integration | Mapping[str, Any],
    trigger_type: str,
) -> bool:
    if isinstance(integration, Integration):
        provider = integration.provider
    else:
        provider = str(integration.get("provider") or "")
    return _provider_supports_trigger(provider, trigger_type)
