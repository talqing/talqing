"""Per-provider implementations (constants, identity, OAuth flows, channel adapters).

Import a specific provider — do not star-import this package::

    from services.integrations.providers.asana import AsanaOAuthFlow, ASANA_MCP_URL
    from services.integrations.providers.telegram import TelegramChannelAdapter

Product catalog (labels, capabilities, setup fields) lives in
``services.integrations.catalog``. Channel adapter protocol: ``channel.py``.
"""
