# Integrations (`services.integrations`)

Provider definitions, OAuth helpers, provider clients, and business ops live
under this package. Import the public surface from the package root:

```python
from services.integrations import Integration, IntegrationResponse, create_integration
from services import integrations

await integrations.create_integration(body, ctx)
```

| Concern | Where |
|---|---|
| **Shared literals** | `types.py` — auth type, status, agent channel |
| **Provider catalog (source of truth)** | `catalog.py` — labels, auth type, capabilities, tool source (`hosted_mcp` / `native_tools`), catalog fields |
| **Internal domain models** | `models.Integration`, `models.IntegrationTrigger` — use everywhere internally |
| **Public API shapes** | `IntegrationResponse`, `IntegrationTriggerResponse` — HTTP / CoPilot only (`.to_response()`) |
| Request + other response shapes | `models.py` (depends on `types`; `Integration` may import catalog at runtime) |
| Config validation | `definitions.py` |
| OAuth subsystem | `oauth/` — shared engine; see layer table below |
| Provider implementations | `providers/{name}.py` — **all** provider-specific logic |
| CRUD / MCP tool list load | `service.py` |
| Trigger setup + runtime active-trigger load | `triggers.py` |
| Channel adapters (send + lifecycle) | `channel.py` + `providers/telegram.py` |
| External conversation keys | `services.conversations.refs` (`conversation_refs`) |
| Runtime tool wiring | `compiler/integrations.py` (takes `Integration`) |
| Tools we implement ourselves | `compiler/native/{provider}.py` |

## OAuth layers (imports only point downward)

```
oauth/protocol.py     — types, PKCE, discovery, shared helpers
         ↑
oauth/dcr.py          — deployment-wide DCR client cache (control DB)
         ↑
providers/{asana,hubspot,…}.py  — *OAuthFlow classes live here
         ↑
oauth/registry.py
         ↑
oauth/credentials.py  — encrypted tokens + oauth_metadata + refresh
         ↑
oauth/session.py      — start/complete + finalize + redirects
```

| Module | Owns |
|---|---|
| `oauth/protocol` | Protocol, result types, PKCE, RFC 9728/8414 discovery, flow utils |
| `oauth/dcr` | Stable Dynamic Client Registration (`client_id` per provider+redirect) |
| `providers/{name}.py` | That provider’s constants, identity, and `*OAuthFlow` class |
| `oauth/registry` | Lookup table of flow classes → `get_oauth_flow` |
| `oauth/credentials` | Encrypted token storage, `oauth_metadata`, refresh (incl. force) |
| `oauth/session` | Connect HTTP path (`start_oauth` / `complete_oauth` / finalize) |

### OAuth credential fields

| Field | Purpose |
|---|---|
| `access_token` / `refresh_token` | Fernet-encrypted tokens |
| `oauth_client_id` | Static app client id, or DCR `client_id` |
| `oauth_metadata` | Connection-time discovery: `token_endpoint`, `resource`, `revocation_endpoint` |

Refresh **must** prefer `oauth_metadata` over hardcoded constants so endpoint drift
cannot break long-lived connections.

### DCR providers (Calendly, Jira)

Vendor docs treat `client_id` as **stable and long-lived**. We register once per
`(provider, redirect_uri)` in the control-plane table `oauth_dcr_clients` and
reuse for every tenant connect. Do not re-register on every Connect click.

### Runtime 401 handling

`compiler/integrations._OAuthOutboundAuth` forces one token refresh and retries
on HTTP 401 before marking the integration `needs_reconnect` (covers revoked
access tokens that are still within `expires_at`). It is the `httpx.Auth` for
both compile paths — a hosted MCP server and our own REST calls alike.

## Where a provider's tools come from

Two paths, and `ProviderSpec` says which by setting exactly one of `hosted_mcp`
or `native_tools`. Both end at a `llm.Toolset` of raw-schema tools carrying the
exposed names, so `list_mcp_tools`, the tool-approval modal and dispatch are
identical either way.

| Path | Flag | Built by | Providers |
|---|---|---|---|
| Vendor-hosted MCP server | `hosted_mcp=HostedMcp(url=…)` | `compiler/integrations.py` | Asana, Cal.com, Calendly, Jira, HubSpot, RocketReach, Exa, Tavily, Resend |
| Our own tools on the vendor's API | `native_tools=True` | `compiler/native/` | Google Calendar |
| A tenant's own MCP server | neither (`custom_mcp`) | `compiler/integrations.py` | `custom_mcp` |

**Hosted MCP is the default and should stay it** — one URL, and the vendor owns
the surface. Going native is worth it when the hosted server is the problem
rather than the shortcut. Google Calendar's was: preview-only (so unshippable),
it rejects `calendar.events.owned` on every write while its own REST API accepts
it, and it puts a second network hop inside a turn a caller is waiting out.

## Adding a provider

### Native tools (like Google Calendar)

1. Constants, identity and the `*OAuthFlow` stay in `providers/{provider}.py` —
   connecting is unchanged by this choice
2. `ProviderSpec` with `native_tools=True` and **no** `hosted_mcp`
3. `compiler/native/{provider}.py` builds an `llm.Toolset` of
   `function_tool(dispatcher, raw_schema=…)`, and registers it in
   `compiler/native/__init__.NATIVE_TOOL_BUILDERS`
4. Apply `allowed_tools` on the tool's own name, then `tools_namespace` via
   `services.tools.exposed_tool_name` — in that order
5. Raise `ToolError` naming the argument at fault for anything the model could
   fix. That is most of why writing the tools is worth it
6. Close the HTTP client in `Toolset.aclose()`

### Hosted OAuth MCP (like Asana)

1. Everything Asana-specific in `providers/asana.py` (constants, identity, `AsanaOAuthFlow`)
2. `ProviderSpec` entry in `catalog.PROVIDERS` (`auth_type="oauth"`, `hosted_mcp=…`)
3. Register the flow class in `oauth/registry.py` (`_FLOW_CLASSES`)
4. Env settings for static-client providers (if needed)
5. Persist `oauth_metadata` (at least `token_endpoint`, and `resource` when used)

No new route file — `GET /v1/integrations/oauth/{provider}/start|callback` is generic.

### Manual hosted MCP (like Exa / Tavily / Resend)

1. Constants in `providers/{provider}.py` if needed
2. `ProviderSpec` with `auth_type="manual"` + `hosted_mcp=HostedMcp(url=…)` + optional
   `SetupField(target="credentials_ref")` for the API key
3. Add a provider-specific builder in `compiler/integrations.py` that applies
   `credentials_ref` (header name, Bearer, etc.). Do **not** put the API key in
   `mcp_config`

### Channel / trigger (like Telegram)

1. `ProviderSpec` in `catalog.PROVIDERS` (`auth_type="manual"` + setup_fields + triggers)
2. Primary token on `credentials_ref`; inbound verify secrets on `webhook_config`
3. Implement `ChannelAdapter` in `providers/{name}.py` and register it
   (see `channel.py`) — outbound send + typing hint + optional managed webhook
   lifecycle. `send_outbound` **must** split to that provider's message length
   limit with `channel.split_message_text` and send the parts in order; raise
   `PartialSendError` if a part fails after earlier ones landed. `keep_typing`
   runs for the length of a turn and owns its own refresh cadence
4. Inbound process/resolve for that provider lives in the same module
5. Shared thread/item helpers: `services.messaging.inbound`; delivery orchestrator:
   `services.messaging.delivery` (dispatches to the adapter)
6. Webhook POST on the same `ChannelAdapter` (`receive_webhook`)
7. HTTP: `POST /v1/integrations/webhooks/{provider}/{tenant_id}/{integration_id}`
   (adapter dispatch)

### Trigger reply modes

| Mode | Behavior |
|---|---|
| `public_reply` | Adapter sends a customer-visible message |
| `internal_note` | Adapter sends via provider private-note API when supported; otherwise skip |
| `none` | Never send (generate only) |

The Telegram Bot API has no internal-note surface — only `public_reply` and
`none` are effective there today.

### Typing indicators

`ChannelAdapter.keep_typing` runs as a background task for the length of a turn
(driver: `services.messaging.typing_indicator`) so a channel customer is not
left staring at nothing while the agent thinks and runs tools.

| Provider | Call | Lifetime | Notes |
|---|---|---|---|
| Telegram | `sendChatAction(action=typing)` | 5s | refreshed every 4s until the turn ends |

Skipped when the trigger's `reply_mode` is `none` — providers ask that the hint
only be shown when a reply is actually coming. Failures are logged and never
surface into the turn.

## Credential placement

| Role | Home |
|---|---|
| OAuth tokens + refresh metadata | `integration_oauth_credentials` only |
| Primary non-OAuth account secret (bot token, Meta access token, Exa/Tavily API key) | `credentials_ref` only (`{{secrets.NAME}}` at rest) |
| Inbound webhook verification secrets | `webhook_config` secret refs at rest |
| custom MCP server shape (url, headers) | `mcp_config` only |
| Connected-account identity | `provider_account_info` (never secrets) |

Compile path (`compiler/integrations.py`):

- **Native providers** — same OAuth helper, handed to `compiler/native` as the
  `httpx.Auth` for the vendor's own API
- **OAuth hosted MCP** — shared OAuth helper + `HostedMcp.url`
- **Exa / Tavily / Resend** — explicit builders + a **required**
  `credentials_ref`. Tavily and Resend 401 every unauthenticated request
  (measured; Tavily's docs describe a keyless tier its MCP server does not
  have). Exa does answer keyless, but on a shared pool whose rate limit
  strangers exhaust — a keyless agent passes testing and fails mid-call
- **custom_mcp** — user `mcp_config` url/headers
- No `auth_mode` enum; wire auth is provider-specific code

## Schema notes

- **`auth_type` is never stored** — fixed per provider (`oauth` | `manual`), derived
  from `ProviderSpec` on catalog and integration responses. It describes the
  **connect lifecycle**, not MCP transport and not channel vs tools.
- **`HostedMcp`** is location only (`url`, `transport_type`). Runtime auth is
  built in the compiler from provider + credentials.
- **Capabilities are never stored** — always derived from `ProviderSpec.capabilities()`
  as a typed `IntegrationCapabilities` (fixed shape, including catalog `triggers`).
- **Triggers are catalog-driven** — title, description, and required agent channel
  live on `TriggerSpec` in `catalog.py`. Clients must not hardcode trigger lists.
- **`allowed_tools`** — the approved MCP tools, a `TEXT[]` column on
  `integrations`. `NULL` exposes every tool the server lists; a list is the exact
  set attached agents may call. Never stored empty: the API rejects `[]`, because
  `MCPServerHTTP` reads an empty allowlist as *no filter* — the opposite of what
  it says. The compiler passes it as `allowed_tools`; `list_mcp_tools` deliberately
  does not (`approved_only=False`), since approval is chosen from the full list.
  A native toolset applies it itself, on the same names.
- **`tools_namespace`** — the prefix this server's tools are presented to the
  model under, as `<tools_namespace>_<tool>`. `TEXT NOT NULL DEFAULT ''`, and
  `''` is a real value meaning "the server's own names, unprefixed" — the
  default for `custom_mcp` (there is no name to derive one from) and the opt-out
  for any provider. Hosted providers default to the provider key. **Approval
  filters on the server's own names; the prefix is applied after** — so
  `allowed_tools` never changes when the namespace does. The one rule lives in
  `services/tools/defs.py::exposed_tool_name` — next to `ToolsNamespace`, because
  both compile paths apply it — and it also refuses to double-prefix
  (`tavily_search` stays `tavily_search`). Two attached servers sharing a
  non-blank namespace is an agent publish error.
- OAuth tokens live in `integration_oauth_credentials` (Fernet-encrypted).
- At rest, credential fields are always `{{secrets.NAME}}` references, never
  plaintext. Create/patch accept either a secret ref or plaintext; plaintext is
  materialized into the secrets store under a preferred name (or
  `NAME_<hex>` if that name already exists) before validation/persist.
- custom MCP header values are not auto-materialized — paste
  `{{secrets.NAME}}` after creating secrets on the Secrets page.

### `provider_account_info` (identity bag)

Connected-account identity, not credentials. Uniqueness keys:

| Provider family | Key |
|---|---|
| Telegram | `bot_id` |
| Google / Cal.com / Asana | `provider_subject` (account id / sub) |
| HubSpot | `provider_subject` = portal `hub_id` (fallback: user id) |
| Calendly / Jira | `provider_subject` from identity API or JWT |

Display-only fields may also live here (`account_email`, `bot_username`, …).
Routing and thread keys must use uniqueness keys.

Rule of thumb: **domain + mutations → `services.integrations`**; **HTTP →
`api/dataplane/routes/integrations`**; **live session tools → `compiler/integrations`**,
and their implementation, when it is ours, → `compiler/native`.
