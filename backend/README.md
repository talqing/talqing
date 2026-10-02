# Backend

Python APIs + runtime workers for Talqing. Dependencies: `uv` +
`pyproject.toml`. Process image: `Dockerfile` (shared by both APIs and all
workers).

**Two applications, two planes.** `api.control` is the deployment's single
control plane — identity, organizations, tokens, payments and the region list —
and `api.dataplane` is one region's API, which is the published one. They are
separate FastAPI apps with separate router sets, built at import time, so a route
that does not belong to a host is simply not mounted there. Everything under
`services/`, `compiler/` and `workers/` is regional; the handful of
modules that are control's say so in their docstring.

## Mental model

> **`services/`** holds product nouns: pure domain + tenant mutations shared by
> the dashboard and the CoPilots (same pattern as agents, tools, integrations, …).
> **`api/`** is HTTP only.
> **`compiler` + `workers`** are runtime.
> **`utils/`** is only what every process needs and no product noun owns.

If a change does not fit that sentence, put it in the wrong package.

## Layout

| Path | Role |
|---|---|
| `api/core/` | Shared: `create_app()`, the one error shape, credential reading, role deps |
| `api/dataplane/` | The regional API — **the published one**; `openapi/openapi.json`, both SDKs and `mcp/tools.json` come from here |
| `api/control/` | The control API — identity, orgs, tokens, payments, regions. Internal; generates only `frontend/lib/control/` |
| `services/` | Business ops + pure domain per product noun (user, agents, tools, catalog, webhooks, …) |
| `settings/` | Nested Pydantic `Settings` loaded from YAML (`get_settings`) |
| `configs/` | Per-**node** YAML — `{local,dev,prod}.{control,<region>}.config.yaml`, selected by `ENV` |
| `db/` | asyncpg pools (control + tenant data-plane) |
| `utils/` | Process utilities: crypto, `bg`, `cache`, latency |
| `services/messaging/` | Multi-provider **text** channels (inbound/delivery/Kafka + turn SSE contract) |
| `compiler/` | Agent definition → LiveKit `AgentSession` |
| `workers/` | Runtime processes — see [`workers/README.md`](workers/README.md) |
| `migrations/` | Control + tenant data SQL (`control/`, `data/`) |

## Where do I look?

| I want… | Open |
|---|---|
| HTTP endpoint | `api/dataplane/routes/…` (thin); logic in `services/…` |
| Sign-in, orgs, members, tokens, payments | `api/control/routes/…` — the other app |
| Region ↔ control calls | `services/control/` (region → control), `services/regions/` (control → region) |
| Env / process config | `settings/` + `configs/{env}.{node}.config.yaml` |
| DB pools | `db/` |
| User / Tenant / Context | `services.user` (`User`, `Tenant`, `Context`, roles) |
| Agent config + API shapes | `services.agents` (`AgentConfig`, requests/responses) |
| Agent language / handoff media checks | `services.agents` (`language_name`, `media_signature`, `handoff_media_error`) |
| Agent draft validation | `services.agents` (`validate_agent_draft`) |
| Agent CRUD / publish | `services.agents` (`create_agent`, `publish_agent`, …) |
| Provider catalog (`catalog.yaml` index) | `services.catalog` (`Catalog`, `get_catalog`, pricing entry types) |
| Public catalog / live voice & avatar galleries | `services.catalog` (`get_public_catalog`, `list_voices`, `list_avatars`, …) |
| Session pricing / billing | `services.billing` (`bill_session`, `price_session`, usage models, …) |
| Tool templating / op trees / SSRF / validate | `services.tools` (`resolve`, `check_url_async`, `validate_operation_tree`, …) |
| Tool shapes (TypedDicts) | `services.tools` (`Operation`, `ToolDefinition`, `RuntimeContext`, …) |
| Tool CRUD / publish | `services.tools` (`create_tool`, `publish_tool`, …) |
| Conversation shapes / SSE | `services.conversations` (`ConversationResponse`, `publish`, …) |
| Conversation inbox read / patch / snapshot | `services.conversations` (`list_conversations`, `get_event_snapshot`, …) |
| Chats: start / message / end / browser token | `services.chats` (`open.start_chat`, `service.send_message`, …) |
| Call token / list / detail / stats | `services/calls/` |
| CoPilot identity / timeline / agent | `services.copilot` (`AGENT_COPILOT`, `TOOL_COPILOT`, `CopilotAgent`, …) |
| CoPilot tools (the exposed API *functions*, `api.dataplane.functions`) | `services.copilot` via `build_copilot_functions` |
| CoPilot message enqueue / snapshot | `services.copilot` (`send_message`, `get_snapshot`) |
| CoPilot HTTP | `api/dataplane/routes/copilot.py` (thin; SSE stays in route) |
| Workspace observability | `services/observability/` |
| Organization / membership provisioning | `services.user` (`create_organization`, `add_member`, …) |
| Integration defs / OAuth / providers | `services.integrations` (defs, OAuth/PKCE, provider clients) |
| Integration CRUD / triggers | `services.integrations` (`create_integration`, triggers, …) |
| Integration HTTP / OAuth routes | `api/dataplane/routes/integrations/` (thin; `from services import integrations`) |
| Runtime MCP wiring | `compiler/integrations.py` |
| Webhook events / delivery / CRUD | `services.webhooks` (`events`, `dispatch`, `create_webhook`, …) |
| Conversation resolve / SSE / inbox | `services.conversations` |
| Conversation HTTP | `api/dataplane/routes/conversations.py` (thin; SSE stays in route) |
| Call token / list / detail | `services/calls/` + thin `api/dataplane/routes/calls.py` |
| Agent compile / tools at runtime | `compiler/` |
| Text channel rails | `services/messaging/` |
| Telegram channel adapter (text) | `services/integrations/providers/telegram.py` |
| Runtime processes | `workers/README.md` |
| Session transcript / finalize | `workers/session/` |
| Schema | `migrations/{control,data}/` |

## Processes (docker-compose)

| Service | Module |
|---|---|
| `api-control` | `uvicorn api.control.main:app` — one per deployment; in production a droplet of its own |
| `api` | `uvicorn api.dataplane.main:app` — one per region |
| `voice-worker` | `python -m workers.voice.main start` |
| `text-worker` | `python -m workers.text.main` |
| `background-worker` | `python -m workers.background.main` — runs every `scheduled_jobs` kind and nothing else |
| `job-scheduler` | `python -m workers.scheduler.main` — publishes what is due; never runs it |

## Layering

```
settings          →  (env / pydantic only)
db                →  settings
utils             →  settings, db  (crypto, bg, cache, latency)
api.core          →  services.user, settings   (shared by both apps)
api.dataplane     →  api.core, services + domain packages as needed
api.control       →  api.core, db, services.{user,billing,credits}
services          →  utils, settings, db, messaging
                     (user: models, context, provisioning)
                     (agents: models, language, validate, service)
                     (tools: defs, resolve, tree, ssrf, validate, tooling, service)
                     (billing: models, pricing, session)
                     (catalog: models, loader, voices, service)
                     (conversations: models, helpers, events, service)
                     (integrations: models, definitions, oauth, providers, service, triggers)
                     (copilot: subjects, const, models, items, stream, prompt/, agent, tools, service)
                     (webhooks: events, delivery, service)
                     (✗ must not import api.dataplane.routes or api.control.routes)
compiler          →  services.agents, services.catalog, services.integrations, services.tools, settings, workers.session
workers           →  utils, db, settings, services, compiler, domains
```


`Context` (signed-in user + tenant) is imported from `services.user`. FastAPI deps
only resolve auth and inject it — and *where* they resolve it is the clearest
expression of the split: `api.control.deps` reads `users ⋈ memberships ⋈ tenants`
from its own database, `api.dataplane.deps` calls `POST /internal/v1/auth-context`
because a region has no such database and must not. Both are built from
`api.core.deps.context_deps`, and neither caches — deleting a membership 401s the
very next request.

### Agents: layers under `services.agents`

| Layer | Module | Role |
|---|---|---|
| Pure domain | `models` / `language` / `media` | Config structs, language checks, avatar/handoff media |
| Business ops | `service` / `validate` | DB CRUD, publish, draft validation (import via `services.agents`) |
| Runtime | `compiler` + `workers/session` | Compile + load + execute |

### Tools: layers under `services.tools`

| Layer | Module | Role |
|---|---|---|
| Pure domain | `defs` / `resolve` / `tree` / `ssrf` / `validate` | TypedDicts, templating, tree shape, SSRF, publish fields |
| Business ops | `service` | DB CRUD + publish |
| Public surface | `__init__` | Re-exports pure + business API (`from services.tools import …`) |
| Runtime compile | `compiler.tools` / `compiler.operations` | LiveKit function tools + op execution |

### Catalog: layers under `services.catalog`

| Layer | Module | Role |
|---|---|---|
| Pure domain | `models` / `loader` | `catalog.yaml` entry shapes, `Catalog` index, `get_catalog` |
| Live galleries | `voices/` | Provider voice libraries (ElevenLabs, xAI, …) next to their use |
| Business ops | `service` | Public dropdown payload, list voices/avatars, add shared voice |
| HTTP | `api/dataplane/routes/catalog.py` | Thin adapter (`from services import catalog`) |

### Conversations: layers under `services.conversations`

| Layer | Module | Role |
|---|---|---|
| Pure domain | `models` | Inbox / item / run / message request-response shapes |
| Resolve helpers | `helpers` | Run-agent resolution, reopen, web contact/conversation |
| Live events | `events` | Redis pub/sub + SSE frame helpers |
| Business ops | `service` | Inbox CRUD, text message enqueue, event snapshot |
| HTTP | `api/dataplane/routes/conversations.py` | Thin adapter (`from services import conversations`; SSE stays in route) |
| Runtime consumers | `services/messaging/`, `workers/text/` | Publish live events; resolve agents on inbound |

### Integrations: layers under `services.integrations`

| Layer | Module | Role |
|---|---|---|
| Literals | `types` | Auth type, status, agent channel |
| Pure domain | `models` / `definitions` / `catalog` | API shapes, validation, provider catalog |
| Provider implementations | `providers/` (`asana`, `hubspot`, …) | Constants, identity, OAuth flows |
| OAuth engine | `oauth/` (`protocol` → `registry` → `credentials` → `session`) | Shared connect/token machinery |
| Business ops | `service` / `triggers` | CRUD, triggers (import via `services.integrations`) |
| HTTP | `api/dataplane/routes/integrations/` | Thin adapter (`from services import integrations`) |
| Runtime | `compiler/integrations.py` | MCP tools on the live session |

### CoPilots: layers under `services.copilot`

AgentCoPilot edits an agent, ToolCoPilot a tool, TaskCoPilot an agent task.
Everything that differs between them is a field on the `CopilotSubject` in
`subjects`; everything else in this package is shared. Each binds a slice of the
API operation surface, plus web access.

| Layer | Module | Role |
|---|---|---|
| Who each CoPilot is | `subjects` | Sentinel id, ref kind, prompts, function slice |
| Shapes / bounds | `models` / `const` | Request/response + `PublicMessage`, step cap |
| Timeline helpers | `items` / `stream` | Row projection, Redis pub/sub + SSE frames |
| Builder agent | `agent` / `prompt/` / `tools` | LiveKit agent, prompts, operation binding |
| Business ops | `service` | Message enqueue, snapshot (import via `services.copilot`) |
| HTTP | `api/dataplane/routes/copilot.py` | Thin adapter (`from services import copilot`; SSE stays in route) |
| Runtime | `workers/text` | Kafka turns + plane preparers + shared turn loop |

### Webhooks: layers under `services.webhooks`

| Layer | Module | Role |
|---|---|---|
| Pure domain | `events` / `delivery` | Event vocabulary, HMAC signing, best-effort fan-out |
| Business ops | `service` | Tenant webhook config CRUD, test-fire, delivery log |
| Public surface | `__init__` | Re-exports pure + business API (`from services import webhooks`) |
| HTTP | `api/dataplane/routes/webhooks.py` | Thin adapter (`from services import webhooks`) |

### What remains in `utils/`

Crypto, process utils (`bg`, `cache`), and latency helpers.
Identity (`User` / `Tenant` / `Context`) and tenant provisioning live in `services.user`.

## Migrations

Numbered SQL files under `migrations/control/` and `migrations/data/`. The
runner applies them in order per DB. Local development: edit `0001_init.sql`
in place and wipe volumes rather than stacking compatibility migrations
(`docker compose down -v && docker compose up --build`).

## Dev tooling

```bash
cd backend
uv sync --all-groups          # includes dev: ruff, pytest
uv run ruff check .
uv run ruff format .
uv run pytest                 # if/when tests/ is restored
```

## Package map (messaging)

| Concern | Package |
|---|---|
| Inbound text webhooks → Kafka turns | `services.messaging.inbound` |
| Outbound text after a turn | `services.messaging.delivery` |
| Text turn Kafka envelope | `services.messaging.textq` |
| Shared turn SSE payload | `services.messaging.models.TurnPayload` |
| Telegram channel adapter (text + webhook fan-out) | `services.integrations.providers.telegram` |
