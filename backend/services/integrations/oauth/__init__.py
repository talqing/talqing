"""OAuth subsystem.

Provider-specific OAuth lives under ``providers/`` (``providers.asana.AsanaOAuthFlow``, …).

Shared engine (imports only point downward)::

    protocol          ← types, PKCE, discovery, HTTP helpers, flow utilities
        ↑
    dcr               ← deployment-wide Dynamic Client Registration cache
        ↑
    providers/*OAuthFlow
        ↑
    registry          ← flow class lookup
        ↑
    credentials       ← token store / oauth_metadata / refresh
        ↑
    session           ← start/complete + finalize (HTTP routes)

Import from the submodule you need::

    from services.integrations.oauth.protocol import OAuthConnectResult, make_pkce
    from services.integrations.oauth.session import start_oauth, complete_oauth
    from services.integrations.oauth.credentials import get_oauth_access_token
    from services.integrations.oauth.registry import require_oauth_provider_path

This package ``__init__`` is intentionally empty so provider modules can import
``oauth.protocol`` without pulling credentials/session (and creating a cycle).
"""
