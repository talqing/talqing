"""The regional API: everything a tenant's workspace is made of.

This is **the published API**. `openapi/openapi.json`, both SDKs and
`mcp/tools.json` come from this app and nothing else, so an integrator holds one
base URL and one token, and no published method can express a call to the other
plane. The control API beside it is internal and changes without SDK or
versioning obligations — which matters, because regions, billing and the org
model are exactly the surfaces most likely to churn.

Run as ``uvicorn api.dataplane.main:app``. The container's command says what it
is; there is no environment variable that turns this into the control app, and
the assertion below refuses a control config outright rather than starting and
failing somewhere later.
"""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI

import db
from api.core.app import create_app, iter_routes  # noqa: F401  (iter_routes: re-exported)
from api.dataplane import sdk_surface
from api.dataplane.routes import (
    agents,
    billing,
    byok,
    calls,
    catalog,
    chats,
    conversations,
    copilot,
    email,
    faqs,
    integrations,
    internal,
    observability,
    schemas,
    secrets,
    streams,
    tasks,
    telephony,
    tools,
    webhooks,
    whatsapp,
)
from migrations.runner import migrate_data
from services import catalog as catalog_service
from services.chats.models import ChatTurnResultEvent
from services.control import client as control
from services.messaging import textq
from services.telephony import livekit_sip
from settings import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("talqing.api")

_settings = get_settings()
if _settings.role != "region":
    # At import, so the wrong pairing is a process that never starts rather than
    # one that starts and 500s on its first request. The filename is the first
    # line of defence (a region's config is simply not found under a control
    # ENV); this is the second.
    raise RuntimeError(
        f"api.dataplane is the REGIONAL app but ENV selected a {_settings.role} config — "
        "run `uvicorn api.control.main:app` for the control plane"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Idempotent, and scoped to what this region owns: `migrations/data/` applied
    # to every DSN in `db.data_dsns()`. Control's schema is control's to migrate;
    # this process could not reach it if it tried.
    await migrate_data()
    logger.info("data-plane migrations applied (region %s)", _settings.region)
    # The models a `browse: search` provider offers, refreshed in the background
    # for as long as this process serves. Nothing waits on it: until the first
    # fetch lands those models simply are not in the catalog, which is what the
    # editor and publish validation will say.
    registry = catalog_service.start_model_registry()
    try:
        yield
    finally:
        if registry is not None:
            registry.cancel()
        await textq.close()
        await livekit_sip.aclose()
        await control.aclose()
        await db.close_all()


# The operations that refuse a workspace with no credit left. Named here rather
# than derived, because "starts a billable call" is a property of what the
# operation DOES and not of the route's shape — and 402 is a code a caller writes
# a real branch for, so leaving it to `default` would hide the one error that has
# a specific fix. Two of the four credit gates are operations a caller invokes;
# the other two — a batch's dialling pass and inbound PSTN — run in the
# background and reach no HTTP caller to answer.
PAYMENT_REQUIRED_ROUTES = frozenset({"calls_token", "create_outbound_call", "create_chat"})

# The four routes a browser's chat token opens, which is what makes them the
# only ones answered to an origin other than the dashboard's.
CHAT_TOKEN_PATHS = re.compile(r"/v1/chats/[^/]+(/(items|messages|end))?")

app = create_app(
    title="Talqing API",
    summary="Public API for building, publishing, and operating Talqing AI agents.",
    version="1.0.0",
    lifespan=lifespan,
    surface=sdk_surface.SURFACE,
    payment_required_routes=PAYMENT_REQUIRED_ROUTES,
    extra_schemas=(ChatTurnResultEvent,),
    any_origin_paths=CHAT_TOKEN_PATHS,
    # `/health` and the redirects/webhooks, which are `include_in_schema=False`
    # and so are listed for intent rather than effect.
    public_paths={"/health"},
    not_found_hint=(
        "No such operation on this region's API. Organizations, members, tokens and "
        f"sign-in live on the control plane at {_settings.control.api_url}."
        if _settings.control
        else "No such operation on this region's API."
    ),
    tags=[
        {
            "name": "catalog",
            "description": "Provider, model, avatar, and voice catalogs.",
        },
        {
            "name": "schemas",
            "description": (
                "Full definitions of the large request types that an AI builder's "
                "tool arguments defer to."
            ),
        },
        {
            "name": "agents",
            "description": "Agent drafts, published versions, and runtime tokens.",
        },
        {
            "name": "calls",
            "description": "Call tokens, call history, usage, and cost observability.",
        },
        {
            "name": "chats",
            "description": "Text sessions with an agent: start, message, end.",
        },
        {
            "name": "conversations",
            "description": "Persistent customer conversations across channels.",
        },
        {
            "name": "observability",
            "description": "Workspace execution, cost, and latency analytics.",
        },
        {"name": "tools", "description": "Custom tools and operation trees."},
        {
            "name": "tasks",
            "description": "Agent tasks: a prompt, tools and a typed result, with nobody to talk to.",
        },
        {
            "name": "email",
            "description": "Verified senders, and email batches reviewed row by row before sending.",
        },
        {
            "name": "whatsapp",
            "description": "Approved templates, and WhatsApp batches sent one template per row.",
        },
        {
            "name": "integrations",
            "description": "External services and MCP servers attached to agents.",
        },
        {
            "name": "telephony",
            "description": (
                "SIP carrier accounts, phone numbers, PSTN calling, and WebSocket media "
                "streams from a partner's platform."
            ),
        },
        {
            "name": "faqs",
            "description": "Question/answer lists that agents and tasks answer from.",
        },
        {"name": "secrets", "description": "Tenant-scoped secret storage for tools."},
        {
            "name": "byok",
            "description": "BYOK: the tenant's own API key for each AI provider.",
        },
        {
            "name": "copilot",
            "description": "AgentCoPilot and ToolCoPilot builder conversations.",
        },
        {
            "name": "billing",
            "description": (
                "Prepaid credits: the balance, its ledger, and buying more. Held per "
                "region, so this is this region's balance."
            ),
        },
        {"name": "webhooks", "description": "Webhook subscriptions and delivery logs."},
        {"name": "service", "description": "Liveness of the API itself."},
    ],
    routers=[
        catalog.router,
        schemas.router,
        agents.router,
        # Before the calls router on purpose: its `GET /v1/calls/{session_id}`
        # would otherwise capture "batches" and refuse it as a malformed UUID.
        telephony.batches_router,
        calls.router,
        chats.router,
        conversations.router,
        observability.router,
        tools.router,
        tasks.router,
        email.router,
        whatsapp.router,
        integrations.router,
        faqs.router,
        telephony.router,
        telephony.outbound_router,
        streams.router,
        secrets.router,
        byok.router,
        copilot.router,
        billing.router,
        webhooks.router,
    ],
    # Control -> region, at /internal/v1 rather than under /v1. Hidden from the
    # document: authenticated by a deployment credential, not a tenant's.
    internal_routers=[internal.router],
)
