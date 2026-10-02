"""Anam stock avatar gallery."""

from __future__ import annotations

import logging

import httpx
from fastapi import HTTPException

from ..models import AvatarItemResponse, AvatarsResponse
from .common import (
    HTTP_TIMEOUT_SECONDS,
    ProviderCredential,
    cached,
)

logger = logging.getLogger("talqing.services.catalog")

_PAGE_SIZE = 100
ANAM_API_URL = "https://api.anam.ai"


def _avatar_item(a: dict) -> AvatarItemResponse:
    raw_id = a.get("id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        raise ValueError("Anam avatar payload is missing 'id'")
    avatar_id = raw_id.strip()
    return AvatarItemResponse(
        id=avatar_id,
        name=a.get("displayName") or avatar_id,
        variant=a.get("variantName"),
        image_url=a.get("imageUrl"),
        versions=a.get("availableVersions") or [],
        active_version=a.get("activeVersion"),
        render_style=a.get("renderStyle"),
    )


async def _fetch_all_avatars(api_key: str) -> list[AvatarItemResponse]:
    """Page through the account's full gallery (the stock one is ~120 faces)."""
    avatars: list[AvatarItemResponse] = []
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        page = 1
        while True:
            r = await client.get(
                f"{ANAM_API_URL}/v1/avatars",
                params={"perPage": _PAGE_SIZE, "page": page},
                headers={"Authorization": f"Bearer {api_key}"},
            )
            r.raise_for_status()
            raw = r.json()
            for a in raw.get("data") or []:
                if not a.get("id"):
                    continue
                avatars.append(_avatar_item(a))
            next_page = (raw.get("meta") or {}).get("next")
            if not next_page:
                break
            page = int(next_page)
    return avatars


async def list_avatars(
    cred: ProviderCredential,
    *,
    active_version: str | None = None,
    render_style: str | None = None,
    offset: int = 0,
    limit: int = 100,
) -> AvatarsResponse:
    """Anam avatar gallery, optionally filtered by Cara version and style.

    These are live gallery facets (Anam ``activeVersion`` / ``renderStyle``),
    not catalog billing models — the billable SKU is always anam/anam.
    Facet option lists are built from the full index before filters so
    dropdowns stay stable while filtering. ``offset``/``limit`` slice the
    filtered list.
    """
    active_version = (active_version or "").strip() or None
    render_style = (render_style or "").strip().lower() or None
    offset = max(0, offset)
    limit = max(1, min(limit, 100))

    async def fetch() -> list[AvatarItemResponse]:
        try:
            return await _fetch_all_avatars(cred.api_key)
        except Exception:
            logger.exception("Anam gallery fetch failed")
            raise HTTPException(status_code=502, detail="anam avatar gallery unavailable")

    avatars = await cached(cred, ("avatars", "anam", "index"), fetch)
    # Version options from the full library; style options after the version
    # filter so the style list matches the faces in view.
    active_versions = sorted({a.active_version for a in avatars if a.active_version})
    if active_version:
        avatars = [a for a in avatars if a.active_version == active_version]
    render_styles = sorted({(a.render_style or "").lower() for a in avatars if a.render_style})
    if render_style:
        avatars = [a for a in avatars if (a.render_style or "").lower() == render_style]
    # Same shape as list endpoints (agents/conversations): has_more only, no total.
    page = avatars[offset : offset + limit + 1]
    has_more = len(page) > limit
    return AvatarsResponse(
        avatars=page[:limit],
        active_versions=active_versions,
        render_styles=render_styles,
        has_more=has_more,
        workspace_key=cred.is_workspace_key,
    )
