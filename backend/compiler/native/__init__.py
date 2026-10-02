"""Providers whose agent tools we implement ourselves, against their own API.

The alternative — and what every other tool-bearing integration still does — is
to point LiveKit's MCP client at a server the vendor hosts. That is the right
default: it is one URL and the vendor owns the surface. A provider moves here
when the hosted server is the problem rather than the shortcut, which for Google
Calendar meant a preview-only server we may not ship on, that rejects the scope
its own REST API accepts, and that puts a second network hop inside a turn a
caller is waiting out.

What a native toolset must keep, because everything downstream assumes it:

- **raw-schema function tools.** Byte-identical in shape to what LiveKit's MCP
  client produces, which is why ``list_mcp_tools`` and the tool-approval modal
  need no branch for this path.
- **``allowed_tools`` filtered on the tool's own name**, and ``tools_namespace``
  applied after it (``services.tools.exposed_tool_name``). Approval must not
  change meaning when the namespace does.
- **``ToolError`` for anything the model could fix**, naming the argument at
  fault. That is the whole reason this is worth writing rather than proxying.

Auth is built by the caller (`compiler.integrations`) and handed in as an
``httpx.Auth``, so nothing here reads credentials or knows the OAuth tables.
"""

from __future__ import annotations

import httpx
from livekit.agents import llm

from .google_calendar import GoogleCalendarToolset

__all__ = ["NATIVE_TOOL_BUILDERS", "build_native_toolset"]


def _google_calendar(
    *, toolset_id: str, auth: httpx.Auth, allowed_tools: list[str] | None, namespace: str
) -> llm.Toolset:
    return GoogleCalendarToolset(
        id=toolset_id, auth=auth, allowed_tools=allowed_tools, namespace=namespace
    )


# Provider key → builder. `catalog.ProviderSpec.native_tools` is the flag; this
# is the implementation it promises, and `build_native_toolset` below is what
# makes disagreeing between the two a startup error rather than a silent
# tool-less agent.
NATIVE_TOOL_BUILDERS = {"google_calendar": _google_calendar}


def build_native_toolset(
    provider: str,
    *,
    toolset_id: str,
    auth: httpx.Auth,
    allowed_tools: list[str] | None,
    namespace: str,
) -> llm.Toolset:
    """Compile one natively-implemented provider's tools. KeyError if unknown."""
    builder = NATIVE_TOOL_BUILDERS[provider]
    return builder(
        toolset_id=toolset_id, auth=auth, allowed_tools=allowed_tools, namespace=namespace
    )
