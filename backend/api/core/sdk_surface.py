"""One dotted id per route, named in exactly one place — per app.

The dots are resource nesting. An OpenAPI generator reads ``agents.versions.rollback``
as a path through the client, so that operation becomes
``client.agents.versions.rollback(...)`` in the TypeScript SDK and the equivalent
in every other generated one. Naming the surface in the document every client is
generated from is what keeps the languages from each inventing their own.

There are two surfaces because there are two documents:
``api.dataplane.sdk_surface`` is the published one — the regional API, which is
what ``clients/`` and ``mcp/tools.json`` are built from — and
``api.control.sdk_surface`` is internal, generating only the dashboard's control
client. The same discipline applies to both, because the generator reads the dots
either way.

Route names are untouched by anything here, so MCP tool names
(``mcp/tools.json`` is keyed on the route name) and
``api.dataplane.functions.EXPOSED`` are unaffected. ``api.core.app`` maps between
the two.

A route missing from its app's surface fails at import: an endpoint nobody named
must not reach a document with a name a generator invented for it.
"""

from __future__ import annotations

from collections import Counter


class SdkSurface:
    """One app's route-name <-> operation-id mapping, checked at construction."""

    def __init__(self, plane: str, surface: dict[str, str]) -> None:
        self._plane = plane
        self._surface = surface
        self._route_names = {sdk: name for name, sdk in surface.items()}
        if len(self._route_names) != len(surface):
            counts = Counter(surface.values())
            clashing = sorted(sdk for sdk, n in counts.items() if n > 1)
            raise RuntimeError(f"two {plane} routes share an SDK id: {', '.join(clashing)}")

    def sdk_id(self, route_name: str) -> str:
        """The operation id published for one route."""
        try:
            return self._surface[route_name]
        except KeyError:
            raise RuntimeError(
                f"route '{route_name}' has no entry in api.{self._plane}.sdk_surface. "
                "Add one — it decides where the operation lands in every generated "
                "client (e.g. 'agents.versions.rollback')."
            ) from None

    def route_name(self, sdk_id: str) -> str:
        """The route behind one published operation id."""
        return self._route_names[sdk_id]

    def __len__(self) -> int:
        return len(self._surface)
