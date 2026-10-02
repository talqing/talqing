"""The control API's operation ids — internal, and published nowhere.

The control app is not in `openapi/openapi.json`, in either SDK, or in
`mcp/tools.json`. It gets its own document for exactly one consumer: the
dashboard, whose client is generated into `frontend/lib/control/` by the same
generator that builds the published one. Hand-maintaining 17 endpoints and their
request and response types in the frontend is the drift `services/user/tenants.py`
exists to warn about.

The same discipline as the published surface, because a route missing from this
map still fails at import and the generator still reads the dots as resource
nesting.
"""

from __future__ import annotations

from api.core.sdk_surface import SdkSurface

SDK_SURFACE: dict[str, str] = {
    "health": "health",
    "list_regions": "regions.list",
    "me": "auth.me",
    "logout": "auth.logout",
    "list_orgs": "orgs.list",
    "create_org": "orgs.create",
    "switch_org": "orgs.switch",
    "patch_org": "orgs.update",
    "leave_org": "orgs.leave",
    "list_members": "orgs.members.list",
    "set_member_role": "orgs.members.setRole",
    "remove_member": "orgs.members.remove",
    "list_invites": "orgs.invites.list",
    "create_invite": "orgs.invites.create",
    "revoke_invite": "orgs.invites.revoke",
    "list_tokens": "tokens.list",
    "create_token": "tokens.create",
    "delete_token": "tokens.delete",
    "mcp_token": "tokens.mcp",
}

SURFACE = SdkSurface("control", SDK_SURFACE)
