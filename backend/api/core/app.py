"""One FastAPI app builder, called twice with two fixed router lists.

**Control and the data plane are separate applications**, not one application
with a role switch and not a runtime 404 gate. A route that does not belong to a
host simply is not mounted there, so ``GET /v1/agents`` on the control host is an
ordinary 404 rather than a branch that had to be got right. Two modules build two
app objects at import time with two fixed router sets, and neither can serve the
other's routes at any value of any variable.

What is genuinely cross-cutting lives here: CORS, the one error shape, the
documented error responses, ``iter_routes``, and ``/health``, which both apps need
because production puts an uptime check on each. Everything that differs — the
routers, the tags, the SDK surface, which paths are public — is an argument.

The one nicety worth keeping from the single-app version is the message: there is
still exactly one definition of "not found" rather than one per route, and each
app's now says which plane it is and names the other. That is a message, not a
mechanism.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Sequence
from typing import Any, Literal

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from api.core.deps import required_role
from api.core.schemas import ErrorBody, ErrorResponse, field_errors
from api.core.sdk_surface import SdkSurface
from services.catalog import openrouter
from settings import get_settings

API_VERSION_PREFIX = "/v1"


def iter_routes(container: Any) -> Iterator[APIRoute]:
    """Every APIRoute reachable from an app or router.

    FastAPI 0.139 stopped flattening included routers into ``app.routes`` —
    each one is now a wrapper holding the router it was built from — so walking
    a single level finds nothing but ``/health``. Descending through both shapes
    keeps this working either way. Paths come from the OpenAPI document, not
    from here, so it does not matter that a nested route's own path is missing
    its mount prefix. Also used by ``api.dataplane.functions`` to locate exposed
    routes.
    """
    for route in getattr(container, "routes", ()):
        if isinstance(route, APIRoute):
            yield route
        else:
            yield from iter_routes(getattr(route, "original_router", route))


# The error half of the published contract. The handlers below normalize every
# failure to one body, so the document says it once here rather than in 155
# route decorators — the same reason the normalizing lives in one handler. Each
# code is defined once under `components/responses` and `$ref`d from the
# operations, so a reader sees one definition of "not found" instead of 102
# copies of the same paragraph.
#
# `default` covers whatever is not named on an operation (a 502 from a provider,
# a 503, an unhandled 500); the named codes are the ones a caller writes a
# branch for, and each operation gets exactly the ones its own shape can
# produce (see `_documented_errors`). FastAPI's stock 422 — `HTTPValidationError`,
# whose `detail` is a *list* — is replaced everywhere, because
# `_on_validation_error` below cannot return it.
_ERROR_RESPONSES = {
    "400": (
        "BadRequest",
        "Well-formed, but rejected by a rule; `detail.errors` lists each problem.",
    ),
    "401": (
        "Unauthorized",
        "Missing credential, or one that is revoked or no longer a member of this organization.",
    ),
    "403": (
        "Forbidden",
        "Authenticated, but this organization role may not perform the operation.",
    ),
    "402": (
        "PaymentRequired",
        "This organization has no credit left, so a new call cannot be started. "
        "Calls already running are unaffected.",
    ),
    "404": ("NotFound", "No such resource in this organization."),
    "409": (
        "Conflict",
        "The resource's current state forbids this — a name already taken, a job already running.",
    ),
    "422": (
        "ValidationFailed",
        "The request did not match this operation's schema; `detail.errors` lists each field.",
    ),
    "default": ("Error", "Unexpected error. Every error this API returns carries this body."),
}


def _documented_errors(
    *,
    name: str,
    path: str,
    method: str,
    public: bool,
    role: str,
    has_422: bool,
    payment_required_routes: frozenset[str],
) -> dict[str, dict]:
    """The error responses one operation declares, derived from the route itself.

    Hand-listing them per route would rot the moment a route was added, so the
    codes come from properties the operation already has: whether it is
    authenticated (401), whether it needs a role above VIEWER (403), whether it
    addresses a resource by id (404), and whether it mutates — a mutation is the
    only thing a business rule (400) or the resource's current state (409) can
    reject. Anything else an operation may raise still matches `default`. The one
    exception is 402, which no route shape implies — the caller names those.
    """
    codes = ["default"]
    if has_422:
        codes.append("422")
    if name in payment_required_routes:
        codes.append("402")
    if not public:
        codes.append("401")
        if role in ("write", "admin"):
            codes.append("403")
        if "{" in path:
            codes.append("404")
        if method != "get":
            codes += ["400", "409"]
    return {
        code: {"$ref": f"#/components/responses/{_ERROR_RESPONSES[code][0]}"}
        for code in sorted(codes)
    }


def _flatten_generic_schema_names(schema: dict) -> None:
    """`Page_AgentResponse_` -> `PageAgentResponse`, refs included.

    Pydantic spells a generic instantiation with the brackets replaced by
    underscores, which is a Python detail leaking into a public contract: an SDK
    generator that preserves our schema names hands `Page_AgentResponse_` to
    every tenant, and one that does not preserve them renames the rest of our
    types to fix this one. Flattening here fixes it for every consumer at once.
    """
    schemas = schema["components"]["schemas"]
    renames = {}
    for name in schemas:
        match = re.fullmatch(r"(\w+?)_(\w+)_", name)
        if match:
            renames[name] = f"{match[1]}{match[2]}"
    taken = sorted(set(renames.values()) & set(schemas) - set(renames))
    if taken:
        raise RuntimeError(f"flattening a generic name would collide with: {', '.join(taken)}")

    for old, new in renames.items():
        schemas[new] = schemas.pop(old)
    _repoint_refs(schema, renames)
    # Cheap proof that nothing was missed — a $ref left pointing at a renamed
    # schema is a document that no longer resolves.
    stale = sorted(old for old in renames if f'"{old}"' in json.dumps(schema))
    if stale:
        raise RuntimeError(f"refs still point at renamed schemas: {', '.join(stale)}")


def _repoint_refs(node: object, renames: dict[str, str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and (new := renames.get(ref.rpartition("/")[2])):
            node["$ref"] = f"#/components/schemas/{new}"
        # A discriminator's mapping holds refs too, as plain values.
        mapping = (node.get("discriminator") or {}).get("mapping")
        if isinstance(mapping, dict):
            for key, target in mapping.items():
                if new := renames.get(target.rpartition("/")[2]):
                    mapping[key] = f"#/components/schemas/{new}"
        for value in node.values():
            _repoint_refs(value, renames)
    elif isinstance(node, list):
        for value in node:
            _repoint_refs(value, renames)


class _Cors:
    """Dashboard-only CORS, except on the paths a header credential opens to anyone.

    Everything this API serves is for our own dashboard, which signs in with a
    cookie: its origins are allowlisted and credentials are allowed. The chat
    routes a browser chat token reaches are the exception — the tenant's own
    site calls them, from an origin we cannot know — so on those paths any OTHER
    origin is answered too, and without credentials: the token travels in a
    header, and a cookie must never ride along from a site we did not allowlist.
    The dashboard keeps its credentialed answer on the same paths.
    """

    def __init__(self, app: ASGIApp, *, any_origin_paths: re.Pattern[str] | None) -> None:
        settings = get_settings().app
        self._any_origin_paths = any_origin_paths
        self._dashboard = CORSMiddleware(
            app,
            allow_origins=settings.cors_origins,
            allow_origin_regex=settings.cors_origin_regex,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        self._any_origin = CORSMiddleware(
            app, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"]
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and self._any_origin_paths is not None
            and self._any_origin_paths.fullmatch(scope["path"])
        ):
            origin = Headers(scope=scope).get("origin")
            if origin is not None and not self._dashboard.is_allowed_origin(origin):
                await self._any_origin(scope, receive, send)
                return
        await self._dashboard(scope, receive, send)


class ModelRegistryHealth(BaseModel):
    """How fresh one kind of this process's searched-provider model list is.

    A silently stale registry is the one failure this design trades for
    freshness — a refresh that keeps failing holds the last good snapshot
    indefinitely, on purpose, because clearing it would take every model of that
    provider out of the editor and out of publish validation at once. So its age
    has to be readable without opening a log.
    """

    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    models: int
    # Null until the first refresh has ever landed, which is also when `models`
    # is 0 and no model of that provider resolves.
    age_seconds: float | None = None
    # What the last refresh failed with, or null if it succeeded.
    last_error: str | None = None


class HealthResponse(BaseModel):
    """Liveness of the API process itself."""

    status: Literal["ok"]
    # Per searched kind (`llm`, `stt`), each refreshed on its own. Null on a
    # process that runs no model registry — the control plane, and any region
    # with no `browse: search` provider enabled.
    model_registry: dict[Literal["llm", "stt"], ModelRegistryHealth] | None = None


def create_app(
    *,
    title: str,
    summary: str,
    version: str,
    lifespan,
    routers: Sequence[APIRouter],
    internal_routers: Sequence[APIRouter] = (),
    tags: list[dict[str, str]],
    surface: SdkSurface,
    public_paths: set[str],
    not_found_hint: str,
    payment_required_routes: frozenset[str] = frozenset(),
    extra_schemas: Sequence[type[BaseModel]] = (),
    any_origin_paths: re.Pattern[str] | None = None,
) -> FastAPI:
    """Build one plane's application, with its routers fixed at import time.

    ``extra_schemas`` are models an operation documents outside its
    ``response_model`` — the frames of a stream a JSON route can also answer with
    — which FastAPI would otherwise leave out of ``components``.

    ``any_origin_paths`` are the routes a credential in a header opens to a
    browser on ANY site; see ``_Cors``.
    """

    def operation_id(route: APIRoute) -> str:
        """The published operation id — a dotted SDK path, not the route name.

        Generators read the dots as resource nesting, so this is what decides
        that `rollback_agent_version` is reached as
        `client.agents.versions.rollback(...)`. See the app's own sdk_surface,
        which is also where a new route has to be named.

        A route hidden from the document — the OAuth redirects, the carrier and
        provider webhooks, everything under /internal — reaches no client, so it
        keeps its own name rather than needing an entry that would only ever be
        decoration.
        """
        if not route.include_in_schema:
            return route.name
        return surface.sdk_id(route.name)

    app = FastAPI(
        title=title,
        summary=summary,
        version=version,
        lifespan=lifespan,
        generate_unique_id_function=operation_id,
        openapi_tags=tags,
    )

    app.add_middleware(_Cors, any_origin_paths=any_origin_paths)

    # Every error this API returns has ONE strict shape: {detail: {message, errors}}.
    # Normalizing here (not in each route) lets handlers keep raising
    # `HTTPException(status, "msg")` ergonomically while clients parse `detail`
    # without ever branching on its type. See api.core.schemas.ErrorBody.
    def error_response(
        status_code: int, message: str, errors: list[str], headers=None
    ) -> JSONResponse:
        body = ErrorBody(message=message, errors=errors)
        return JSONResponse(
            status_code=status_code, content={"detail": body.model_dump()}, headers=headers
        )

    @app.exception_handler(StarletteHTTPException)
    async def _on_http_error(request: Request, exc: StarletteHTTPException):
        d = exc.detail
        # a route that raised the structured form (via schemas.validation_error)
        # already carries {message, errors}; a plain string becomes the message
        if isinstance(d, dict) and "message" in d:
            body = ErrorBody.model_validate(d)
            return error_response(exc.status_code, body.message, body.errors, exc.headers)
        # Starlette's own 404 for a path that matched no route at all. On a
        # two-plane deployment that is most often a caller aimed at the wrong
        # host, and saying so costs one sentence.
        if exc.status_code == 404 and d == "Not Found":
            return error_response(404, not_found_hint, [], exc.headers)
        return error_response(exc.status_code, str(d), [], exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(request: Request, exc: RequestValidationError):
        """Request-validation failures (a 422 from a Literal/constraint) read the
        same as any other error — a readable `message` plus per-field `errors`."""
        errors = field_errors(exc.errors())
        # a single problem reads better as the headline message than a generic one
        message = errors[0] if len(errors) == 1 else "invalid request"
        return error_response(422, message, errors)

    for router in routers:
        app.include_router(router, prefix=API_VERSION_PREFIX)
    # The region <-> control link, mounted at its own root rather than under
    # /v1. It is a different surface with a different audience — a deployment
    # credential, not a tenant's — and it versions on its own: /v1 is a promise
    # to integrators, and nothing here is published to one.
    for router in internal_routers:
        app.include_router(router)

    @app.get("/health", tags=["service"], response_model=HealthResponse)
    async def health() -> HealthResponse:
        """Liveness probe: `200` once this process is serving. Unauthenticated.

        It answers for the API process, not its dependencies — a `200` here does
        not promise Postgres, Redis or LiveKit are reachable. The one thing it
        does report is `model_registry`, because that snapshot lives in this
        process's memory and nothing else can see how old it is.
        """
        registry = openrouter.health()
        return HealthResponse(
            status="ok",
            model_registry=None
            if registry is None
            else {
                kind: ModelRegistryHealth(
                    models=health.models,
                    age_seconds=health.age_seconds,
                    last_error=health.last_error,
                )
                for kind, health in registry.items()
            },
        )

    def custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            summary=app.summary,
            routes=app.routes,
            tags=app.openapi_tags,
        )
        components = schema.setdefault("components", {})
        security_schemes = components.setdefault("securitySchemes", {})
        security_schemes.setdefault(
            "BearerAuth",
            {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"},
        )
        security_schemes.setdefault(
            "SessionCookie",
            {
                "type": "apiKey",
                "in": "cookie",
                "name": get_settings().security.session_cookie_name,
            },
        )
        schemas = components.setdefault("schemas", {})
        error = ErrorResponse.model_json_schema(ref_template="#/components/schemas/{model}")
        schemas.update(error.pop("$defs", {}))
        schemas["ErrorResponse"] = error
        for model in extra_schemas:
            extra = model.model_json_schema(
                ref_template="#/components/schemas/{model}", mode="serialization"
            )
            schemas.update(extra.pop("$defs", {}))
            schemas[model.__name__] = extra
        # Nothing points at FastAPI's validation models once every 422 carries
        # ours, and a schema that survives in `components` reads as a shape we
        # return.
        for orphan in ("HTTPValidationError", "ValidationError"):
            schemas.pop(orphan, None)
        components["responses"] = {
            name: {
                "description": description,
                "content": {
                    "application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}
                },
            }
            for name, description in _ERROR_RESPONSES.values()
        }

        roles = {route.name: required_role(route.endpoint) for route in iter_routes(app)}
        for path, methods in schema.get("paths", {}).items():
            public = path in public_paths
            for method, operation in methods.items():
                if not isinstance(operation, dict):
                    continue
                if not public:
                    operation.setdefault("security", [{"BearerAuth": []}, {"SessionCookie": []}])
                responses = operation.setdefault("responses", {})
                name = surface.route_name(operation["operationId"])
                responses.update(
                    _documented_errors(
                        name=name,
                        path=path,
                        method=method,
                        public=public,
                        role=roles[name],
                        has_422="422" in responses,
                        payment_required_routes=payment_required_routes,
                    )
                )
        _flatten_generic_schema_names(schema)
        app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = custom_openapi
    return app
