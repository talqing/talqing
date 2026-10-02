"""Tavily hosted MCP constants and helpers."""

# Docs: https://docs.tavily.com/documentation/mcp
#
# The API key travels as ``Authorization: Bearer tvly-…`` and is REQUIRED: an
# unauthenticated `initialize` gets 401 with a
# `WWW-Authenticate: Bearer resource_metadata=…` challenge, measured 2026-08-28.
# The docs' "keyless" tier covers the REST search API, not the MCP server.

from __future__ import annotations

TAVILY_MCP_URL = "https://mcp.tavily.com/mcp/"
