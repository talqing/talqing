"""The control API: identity, organizations, tokens, payments, and the region list.

One of these exists for the whole deployment, in `fra1`. It holds the global
identity store — every user, every organization, every access token — and it is
the only node that mints a credential or talks to Dodo. Every region calls it
synchronously on every authenticated request and holds a bearer token rather than
a DSN, which is the whole point: a region droplet runs tenant-authored tool code,
and a connection string into that database does not belong on it.

**Internal, and deliberately.** Nothing here is in `openapi/openapi.json`, in
either published SDK, or in `mcp/tools.json`. A coding agent can no longer mint
or revoke a personal access token, and this surface can change without SDK or
versioning obligations. Its one generated consumer is the dashboard's client in
`frontend/lib/control/`.

Run as ``uvicorn api.control.main:app``.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

import db
from api.control import sdk_surface
from api.control.routes import auth, billing, internal, org, regions, tokens
from api.core.app import API_VERSION_PREFIX, create_app
from migrations.runner import migrate_control
from services.regions import client as region_client
from settings import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("talqing.api.control")

_settings = get_settings()
if _settings.role != "control":
    raise RuntimeError(
        f"api.control is the CONTROL app but ENV selected a {_settings.role} config — "
        "run `uvicorn api.dataplane.main:app` for a region"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Control's schema, and only control's: a control process has no data DSN to
    # reach a region's database with, which is what makes that structural.
    await migrate_control()
    try:
        yield
    finally:
        # The pooled clients that push credit to each region, closed the way the
        # dataplane closes its client to here.
        await region_client.aclose()
        await db.close_all()


app = create_app(
    title="Talqing control API",
    summary=(
        "Internal: identity, organizations, access tokens, payments, and the "
        "region list. Not published — the tenant-facing API is regional."
    ),
    version="1.0.0",
    lifespan=lifespan,
    surface=sdk_surface.SURFACE,
    public_paths={
        "/health",
        f"{API_VERSION_PREFIX}/regions",
        # `include_in_schema=False`, so listed for intent rather than effect.
        f"{API_VERSION_PREFIX}/auth/google/start",
        f"{API_VERSION_PREFIX}/auth/google/callback",
        f"{API_VERSION_PREFIX}/auth/logout",
    },
    not_found_hint=(
        "No such operation on the control API. Agents, calls, tools and "
        "billing live on a region: "
        + ", ".join(f"{r.name} at {r.api_url}" for r in _settings.regions)
        + "."
    ),
    tags=[
        {"name": "auth", "description": "Dashboard session authentication."},
        {
            "name": "org",
            "description": "Organizations, their members, roles and invites.",
        },
        {"name": "tokens", "description": "Personal access tokens for API clients."},
        {"name": "regions", "description": "The regions a workspace's resources can live in."},
        {"name": "billing", "description": "The payment processor's callback."},
        {"name": "service", "description": "Liveness of the API itself."},
    ],
    routers=[
        regions.router,
        auth.router,
        org.router,
        org.orgs_router,
        tokens.router,
        tokens.mcp_router,
        billing.router,
    ],
    # Region -> control, at /internal/v1 rather than under /v1. Hidden from the
    # document and authenticated by a per-region deployment credential.
    internal_routers=[internal.router],
)
