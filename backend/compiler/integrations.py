"""Compile published integrations into LiveKit toolsets.

Two sources of tools, and a provider declares which one it is on its
``ProviderSpec``: a server the vendor hosts (``hosted_mcp``), or an
implementation of ours against the vendor's own API (``native_tools``, built in
``compiler.native``). Hosted MCP clients are built with provider-specific code +
credentials (credentials_ref or OAuth table). There is no auth_mode DSL — wire
auth lives next to each provider's URL constants.

Either way the result is a toolset of raw-schema tools carrying the exposed
names, so nothing downstream — approval, the dashboard's tool list, dispatch —
has to know which path an integration took.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from livekit.agents import llm
from livekit.agents.llm.mcp import MCPServerHTTP, MCPTool, MCPToolOptions, MCPToolset
from livekit.agents.llm.tool_context import (
    get_function_info,
    get_raw_function_info,
    is_function_tool,
    is_raw_function_tool,
)

from compiler.native import NATIVE_TOOL_BUILDERS, build_native_toolset
from services.integrations import (
    NATIVE_TOOL_PROVIDER_SET,
    OAUTH_PROVIDER_SET,
    TOOL_PROVIDER_SET,
    OAuthAccessToken,
    get_oauth_access_token,
    hosted_mcp,
    mark_integration_needs_reconnect,
)
from services.integrations.models import Integration
from services.integrations.providers.exa import EXA_MCP_URL
from services.integrations.providers.resend import RESEND_MCP_URL
from services.integrations.providers.tavily import TAVILY_MCP_URL
from services.tools import check_url, exposed_tool_name, resolve
from services.user import Tenant

logger = logging.getLogger("talqing.compiler.integrations")

_MCP_ACCEPT = "application/json, text/event-stream"


class IntegrationCompileError(RuntimeError):
    """Raised when an attached integration cannot be compiled into tools."""


def _toolset_id(integration: Integration) -> str:
    return f"integration_{integration.id}"


# OpenAI compiles a JSON Schema `pattern` with a Rust regex engine, which has no
# lookaround, and rejects the whole request with `invalid_json_schema` when it
# meets one. Tools go up as a single array, so ONE such pattern from ONE server
# kills every turn the agent takes — with every other tool in the request.
# Measured against Resend's MCP server, whose 18 email-taking tools all carry
# `^(?!\.)(?!.*\.\.)…` to reject a leading or doubled dot.
_UNSUPPORTED_PATTERN = re.compile(r"\(\?[=!]|\(\?<[=!]")


def _strip_unsupported_patterns(node: Any, path: str, dropped: list[str]) -> Any:
    """``node`` with every ``pattern`` a model provider cannot compile removed.

    The keyword goes, not the tool: `pattern` only tells the model what shape to
    emit, the MCP server validates the argument itself when the call arrives, and
    the alternative is a tenant whose agent answers nothing at all.

    Applied to every server unconditionally, because this class has no idea which
    LLM the agent runs — a per-provider rule would have to live somewhere that
    does, and the thing being given up is a hint.
    """
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key == "pattern" and isinstance(value, str) and _UNSUPPORTED_PATTERN.search(value):
                dropped.append(f"{path}.{key}")
                continue
            out[key] = _strip_unsupported_patterns(value, f"{path}.{key}", dropped)
        return out
    if isinstance(node, list):
        return [_strip_unsupported_patterns(v, f"{path}[{i}]", dropped) for i, v in enumerate(node)]
    return node


def _resolve_credentials_ref(
    integration: Integration,
    secrets: dict[str, str],
    *,
    provider: str,
    required: bool,
) -> str | None:
    """Resolve credentials_ref. Empty/missing is allowed unless ``required``."""
    configured = integration.credentials_ref
    if not isinstance(configured, str) or not configured.strip():
        if required:
            raise IntegrationCompileError(
                f"{provider} integration {integration.id}: API key is not configured"
            )
        return None
    api_key = resolve(configured, {"secrets": secrets})
    if not isinstance(api_key, str) or not api_key.strip():
        raise IntegrationCompileError(
            f"{provider} integration {integration.id}: API key resolved empty"
        )
    return api_key.strip()


class _MCPServerHTTP(MCPServerHTTP):
    """``MCPServerHTTP`` that speaks HTTP/2 and can carry an ``httpx.Auth``.

    Both are things the base class has no argument for. ``auth`` because an
    OAuth provider mints a fresh access token per request rather than pinning a
    header at build time. HTTP/2 because RocketReach's Cloudflare rule answers
    every HTTP/1.1 request to ``mcp.rocketreach.co/mcp`` with a bot challenge
    instead of the MCP response — the same rule that breaks its OAuth discovery,
    see ``oauth.protocol.oauth_http_client``. It applies to every MCP server we
    connect to, ours and a tenant's own alike: ALPN negotiates it, so a server
    that only speaks HTTP/1.1 still gets HTTP/1.1.

    httpx takes ``http2`` on the client and the base builds that client itself
    with no hook to pass it, so the body below mirrors the base implementation
    rather than delegating to it.

    It is also where ``tools_namespace`` is applied, because ``list_tools`` is
    the one place every consumer of this server goes through.
    """

    def __init__(
        self, *, auth: httpx.Auth | None = None, namespace: str = "", **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self._auth = auth
        self._namespace = namespace

    async def list_tools(
        self, *, tool_options: dict[str, MCPToolOptions] | None = None
    ) -> list[MCPTool]:
        """The server's tools as the model should see them: renamed, and sendable.

        Only the outward schema moves. ``MCPServer._make_function_tool`` closed
        over the server's own name for ``call_tool``, so the wire protocol is
        untouched — and ``super()`` has already applied the ``allowed_tools``
        filter, so approval keeps matching on the names the tenant approved.
        """
        tools = await super().list_tools(tool_options=tool_options)
        dropped: list[str] = []
        for tool in tools:
            # A fresh RawFunctionToolInfo per call — so this edits what we were
            # just handed. `tool.id` reads `info.name` and follows. The nested
            # `parameters` is NOT fresh (it is the cached MCP tool's own
            # inputSchema), which is why the sanitizer copies rather than mutates.
            info = get_raw_function_info(tool)
            schema = info.raw_schema
            if self._namespace:
                info.name = exposed_tool_name(self._namespace, info.name)
                schema = {**schema, "name": info.name}
            params = schema.get("parameters")
            if isinstance(params, dict):
                before = len(dropped)
                clean = _strip_unsupported_patterns(params, info.name, dropped)
                if len(dropped) != before:
                    schema = {**schema, "parameters": clean}
            info.raw_schema = schema
        if dropped:
            # One line per server, not per tool: Resend alone accounts for 18.
            logger.warning(
                "dropped %d unsupported schema pattern(s) from %s: %s",
                len(dropped),
                self.url,
                ", ".join(dropped[:8]) + (" …" if len(dropped) > 8 else ""),
            )
        return tools

    def _create_http_client(
        self,
        headers: dict[str, Any] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        self._http_client = httpx.AsyncClient(
            follow_redirects=True,
            http2=True,
            timeout=timeout
            if timeout is not None
            else httpx.Timeout(self._timeout, read=self._sse_read_timeout),
            headers=headers if headers is not None else self._headers,
            auth=auth if auth is not None else self._auth,
        )
        return self._http_client


def _mcp_toolset(
    toolset_id: str,
    *,
    url: str,
    allowed_tools: list[str] | None,
    namespace: str,
    transport_type: str = "streamable_http",
    headers: dict[str, str] | None = None,
    auth: httpx.Auth | None = None,
) -> llm.Toolset:
    server = _MCPServerHTTP(
        url=url,
        transport_type=transport_type,  # type: ignore[arg-type]
        allowed_tools=allowed_tools,
        namespace=namespace,
        headers={"Accept": _MCP_ACCEPT, **(headers or {})},
        auth=auth,
    )
    return MCPToolset(id=toolset_id, mcp_server=server)


def _build_custom_mcp(
    integration: Integration,
    secrets: dict[str, str],
    *,
    allowed_tools: list[str] | None,
    namespace: str,
) -> llm.Toolset:
    cfg = integration.mcp_config
    if not isinstance(cfg, dict):
        raise IntegrationCompileError(f"integration {integration.id}: invalid mcp_config")

    url = resolve(cfg.get("url") or "", {"secrets": secrets})
    if not isinstance(url, str) or not url.strip():
        raise IntegrationCompileError(f"integration {integration.id}: MCP server URL is missing")
    try:
        check_url(url)
    except ValueError as e:
        raise IntegrationCompileError(f"integration {integration.id}: url rejected ({e})") from e

    headers = {
        k: str(v) for k, v in resolve(cfg.get("headers") or {}, {"secrets": secrets}).items()
    }
    server = _MCPServerHTTP(
        url=url, allowed_tools=allowed_tools, namespace=namespace, headers=headers or None
    )
    return MCPToolset(id=_toolset_id(integration), mcp_server=server)


class _OAuthOutboundAuth(httpx.Auth):
    """Mints the bearer for every outbound request one integration makes.

    Not MCP-specific: a hosted MCP server and our own REST calls to the same
    provider want exactly this, and both get it.

    ``httpx`` runs this on EVERY request — the tool listing at cold start and
    each tool call after it — so the resolved token is held here for as long as
    it is good for rather than read back from the database each time. This
    object belongs to one compiled agent inside one job process, which serves
    one call, so the hold cannot go stale across calls; within the call a token
    revoked before its expiry is what the 401 branch below is for.
    """

    def __init__(self, tenant: Tenant, integration_id: str, provider: str) -> None:
        self._tenant = tenant
        self._integration_id = integration_id
        self._provider = provider
        self._held: OAuthAccessToken | None = None

    async def _bearer(self, *, force_refresh: bool = False) -> str:
        held = self._held
        if not force_refresh and held is not None and held.usable(datetime.now(UTC)):
            return held.token
        # Assigned only on success, so a failed refresh does not drop a token
        # that is still usable.
        self._held = await get_oauth_access_token(
            self._tenant,
            integration_id=self._integration_id,
            provider=self._provider,
            force_refresh=force_refresh,
        )
        return self._held.token

    async def async_auth_flow(self, request: httpx.Request):
        from services.integrations.oauth.protocol import OAuthCredentialError

        request.headers["Authorization"] = f"Bearer {await self._bearer()}"
        response = yield request
        if response.status_code != 401:
            return
        # Access token may be revoked before expiry — force one refresh + retry.
        try:
            bearer = await self._bearer(force_refresh=True)
        except OAuthCredentialError:
            await mark_integration_needs_reconnect(
                self._tenant, integration_id=self._integration_id
            )
            return
        request.headers["Authorization"] = f"Bearer {bearer}"
        response = yield request
        if response.status_code == 401:
            await mark_integration_needs_reconnect(
                self._tenant, integration_id=self._integration_id
            )


def _build_oauth_mcp(
    integration: Integration,
    *,
    tenant: Tenant | None,
    allowed_tools: list[str] | None,
    namespace: str,
) -> llm.Toolset:
    provider = integration.provider
    integration_id = str(integration.id)
    spec = hosted_mcp(provider)
    if spec is None:
        raise IntegrationCompileError(f"{provider}: missing hosted MCP URL")
    if tenant is None:
        raise IntegrationCompileError(
            f"{provider} integration {integration_id} has no tenant context"
        )
    return _mcp_toolset(
        _toolset_id(integration),
        url=spec.url,
        allowed_tools=allowed_tools,
        namespace=namespace,
        transport_type=spec.transport_type,
        auth=_OAuthOutboundAuth(tenant, integration_id, provider),
    )


def _build_exa_mcp(
    integration: Integration,
    secrets: dict[str, str],
    *,
    allowed_tools: list[str] | None,
    namespace: str,
) -> llm.Toolset:
    """Exa hosted MCP: required credentials_ref → x-api-key header."""
    api_key = _resolve_credentials_ref(integration, secrets, provider="exa", required=True)
    return exa_toolset(
        api_key,
        toolset_id=_toolset_id(integration),
        allowed_tools=allowed_tools,
        namespace=namespace,
    )


def exa_toolset(
    api_key: str,
    *,
    toolset_id: str,
    allowed_tools: list[str] | None = None,
    namespace: str = "",
) -> llm.Toolset:
    """Exa's hosted MCP on one key — a tenant's integration, or the CoPilots' own."""
    return _mcp_toolset(
        toolset_id,
        url=EXA_MCP_URL,
        allowed_tools=allowed_tools,
        namespace=namespace,
        headers={"x-api-key": api_key},
    )


def _build_tavily_mcp(
    integration: Integration,
    secrets: dict[str, str],
    *,
    allowed_tools: list[str] | None,
    namespace: str,
) -> llm.Toolset:
    """Tavily hosted MCP: required credentials_ref → Authorization Bearer."""
    api_key = _resolve_credentials_ref(integration, secrets, provider="tavily", required=True)
    return _mcp_toolset(
        _toolset_id(integration),
        url=TAVILY_MCP_URL,
        allowed_tools=allowed_tools,
        namespace=namespace,
        headers={"Authorization": f"Bearer {api_key}"},
    )


def _build_resend_mcp(
    integration: Integration,
    secrets: dict[str, str],
    *,
    allowed_tools: list[str] | None,
    namespace: str,
) -> llm.Toolset:
    """Resend hosted MCP: required credentials_ref → Authorization Bearer."""
    api_key = _resolve_credentials_ref(integration, secrets, provider="resend", required=True)
    return _mcp_toolset(
        _toolset_id(integration),
        url=RESEND_MCP_URL,
        allowed_tools=allowed_tools,
        namespace=namespace,
        headers={"Authorization": f"Bearer {api_key}"},
    )


def _build_native(
    integration: Integration,
    *,
    tenant: Tenant | None,
    allowed_tools: list[str] | None,
    namespace: str,
) -> llm.Toolset:
    """Tools we implement ourselves, over the provider's own API."""
    provider = integration.provider
    integration_id = str(integration.id)
    if tenant is None:
        raise IntegrationCompileError(
            f"{provider} integration {integration_id} has no tenant context"
        )
    if provider not in NATIVE_TOOL_BUILDERS:
        # The catalog promised tools `compiler.native` does not have. A deploy-time
        # bug, and one an agent must not start half-armed on. Checked here rather
        # than caught, so a KeyError from inside a builder stays a KeyError.
        raise IntegrationCompileError(f"{provider}: native tools are not implemented")
    return build_native_toolset(
        provider,
        toolset_id=_toolset_id(integration),
        auth=_OAuthOutboundAuth(tenant, integration_id, provider),
        allowed_tools=allowed_tools,
        namespace=namespace,
    )


def _build_provider_toolset(
    integration: Integration,
    secrets: dict[str, str],
    *,
    tenant: Tenant | None,
    allowed_tools: list[str] | None,
    namespace: str,
) -> llm.Toolset:
    """Dispatch to the provider's builder — native first, then hosted MCP."""
    provider = integration.provider

    if provider in NATIVE_TOOL_PROVIDER_SET:
        return _build_native(
            integration, tenant=tenant, allowed_tools=allowed_tools, namespace=namespace
        )
    if provider == "exa":
        return _build_exa_mcp(
            integration, secrets, allowed_tools=allowed_tools, namespace=namespace
        )
    if provider == "tavily":
        return _build_tavily_mcp(
            integration, secrets, allowed_tools=allowed_tools, namespace=namespace
        )
    if provider == "resend":
        return _build_resend_mcp(
            integration, secrets, allowed_tools=allowed_tools, namespace=namespace
        )
    if provider in OAUTH_PROVIDER_SET:
        return _build_oauth_mcp(
            integration, tenant=tenant, allowed_tools=allowed_tools, namespace=namespace
        )

    raise IntegrationCompileError(f"unknown tool provider: {provider}")


def build_integrations(
    integrations: Sequence[Integration],
    secrets: dict[str, str],
    *,
    tenant: Tenant | None = None,
    approved_only: bool = True,
    namespaced: bool = True,
) -> list[llm.Toolset]:
    """Compile attached integrations. Raises IntegrationCompileError on failure.

    Attached integrations must succeed — silent skips leave agents without tools
    they were configured to use. Always returns toolsets (never bare tools),
    whether the tools come from a hosted MCP server or from `compiler.native`.

    ``approved_only`` applies each integration's ``allowed_tools`` and
    ``namespaced`` applies its ``tools_namespace``. Sessions want both; the
    dashboard's tool-approval list is the one caller that wants neither, because
    it exists to show what has *not* been approved yet, under both names.
    """
    toolsets: list[llm.Toolset] = []
    for integration in integrations:
        provider = integration.provider
        # An approved list is never stored empty (the API rejects that), so a
        # falsy value here always means "no filter" and never "no tools".
        allowed_tools = integration.allowed_tools if approved_only else None
        namespace = integration.tools_namespace if namespaced else ""
        if provider == "custom_mcp":
            toolsets.append(
                _build_custom_mcp(
                    integration, secrets, allowed_tools=allowed_tools, namespace=namespace
                )
            )
            continue
        if provider in TOOL_PROVIDER_SET:
            toolsets.append(
                _build_provider_toolset(
                    integration,
                    secrets,
                    tenant=tenant,
                    allowed_tools=allowed_tools,
                    namespace=namespace,
                )
            )
            continue
        raise IntegrationCompileError(f"unknown integration provider {provider!r}")
    return toolsets


@dataclass(frozen=True, slots=True)
class McpToolDescriptor:
    """Name + description of one integration tool (dashboard preview)."""

    name: str  # the tool's own name — what `allowed_tools` stores
    exposed_name: str  # what the model is shown, and what a prompt must say
    description: str = ""


async def list_mcp_tools(
    integration: Integration,
    secrets: dict[str, str],
    *,
    tenant: Tenant | None = None,
) -> list[McpToolDescriptor]:
    """Every tool one integration offers — from its MCP server, or from ours.

    Deliberately unfiltered by ``allowed_tools``: this is what the tool-approval
    list is built from, so it has to include the tools nobody has approved.
    Unprefixed for the same reason — approval is chosen by the tool's own name —
    with the exposed name computed here so the caller gets both halves.
    Raises IntegrationCompileError when the integration cannot be compiled;
    raises RuntimeError when listing fails after connect.
    """
    toolsets = build_integrations(
        [integration], secrets, tenant=tenant, approved_only=False, namespaced=False
    )
    tools: list[McpToolDescriptor] = []
    for toolset in toolsets:
        try:
            await toolset.setup()
            for item in toolset.tools:
                name = ""
                description = ""
                if is_function_tool(item):
                    info = get_function_info(item)
                    name = info.name
                    description = info.description or ""
                elif is_raw_function_tool(item):
                    info = get_raw_function_info(item)
                    name = info.name
                    description = (info.raw_schema or {}).get("description") or ""
                if name:
                    tools.append(
                        McpToolDescriptor(
                            name=str(name),
                            exposed_name=exposed_tool_name(integration.tools_namespace, str(name)),
                            description=str(description),
                        )
                    )
        except IntegrationCompileError:
            raise
        except Exception as exc:
            raise RuntimeError(f"failed to list MCP tools: {exc}") from exc
        finally:
            try:
                await toolset.aclose()
            except Exception:
                pass
    return tools
