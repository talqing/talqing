"""OAuth flow registry — maps provider id → flow implementation.

Flow classes live under ``providers/`` (``providers.asana.AsanaOAuthFlow``, …).
This module only wires them into a lookup used by credentials and session.
"""

from __future__ import annotations

from fastapi import HTTPException

from ..catalog import OAUTH_PROVIDER_SET, require_provider
from ..providers.asana import AsanaOAuthFlow
from ..providers.cal_com import CalComOAuthFlow
from ..providers.calendly import CalendlyOAuthFlow
from ..providers.google_calendar import GoogleCalendarOAuthFlow
from ..providers.hubspot import HubSpotOAuthFlow
from ..providers.jira import JiraOAuthFlow
from ..providers.rocketreach import RocketReachOAuthFlow
from .protocol import OAuthFlow

# One entry per OAuth provider module. Provider-specific logic stays in that module.
_FLOW_CLASSES: tuple[type, ...] = (
    GoogleCalendarOAuthFlow,
    CalComOAuthFlow,
    CalendlyOAuthFlow,
    AsanaOAuthFlow,
    JiraOAuthFlow,
    HubSpotOAuthFlow,
    RocketReachOAuthFlow,
)

_FLOWS: dict[str, OAuthFlow] | None = None


def _flows() -> dict[str, OAuthFlow]:
    global _FLOWS
    if _FLOWS is not None:
        return _FLOWS
    by_provider = {cls.provider: cls() for cls in _FLOW_CLASSES}
    missing = OAUTH_PROVIDER_SET - set(by_provider)
    if missing:
        raise RuntimeError(f"OAuth flows missing for providers: {sorted(missing)}")
    extra = set(by_provider) - OAUTH_PROVIDER_SET
    if extra:
        raise RuntimeError(f"OAuth flows registered for non-oauth providers: {sorted(extra)}")
    _FLOWS = by_provider
    return _FLOWS


def get_oauth_flow(provider: str) -> OAuthFlow | None:
    if provider not in OAUTH_PROVIDER_SET:
        return None
    return _flows().get(provider)


def require_oauth_flow(provider: str) -> OAuthFlow:
    flow = get_oauth_flow(provider)
    if flow is None:
        raise HTTPException(status_code=404, detail=f"unknown OAuth provider: {provider}")
    return flow


def oauth_provider_configured(provider: str) -> bool:
    flow = get_oauth_flow(provider)
    return bool(flow and flow.is_configured())


def require_oauth_provider_path(provider: str) -> str:
    """Validate path param and return the provider id."""
    if provider not in OAUTH_PROVIDER_SET:
        known = ", ".join(sorted(OAUTH_PROVIDER_SET))
        raise HTTPException(
            status_code=404,
            detail=f"unknown OAuth provider '{provider}'. Known: {known}",
        )
    require_provider(provider)
    return provider
