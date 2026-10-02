"""Exa hosted MCP constants and helpers."""

# Docs: https://docs.exa.ai/reference/exa-mcp
#
# The API key travels as ``x-api-key`` and is REQUIRED by us, not by Exa: the
# server answers keyless with a 200, but on a shared anonymous pool that returns
# "You've hit Exa's free MCP rate limit" once strangers have used it up.

from __future__ import annotations

EXA_MCP_URL = "https://mcp.exa.ai/mcp"
