"""The regions a workspace's resources can live in.

Unauthenticated, because the dashboard reads it before anyone is signed in — the
login page is served by one static build for every region, and this is what tells
it which API hosts exist. Adding a region then needs no dashboard deploy.

There is nothing tenant-specific here and nothing to enable: every region serves
every organization, so this is the same answer for everyone and the switcher
offers all of it.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from settings import get_settings

router = APIRouter(prefix="/regions", tags=["regions"])


class RegionResponse(BaseModel):
    """One region, as the dashboard reads it."""

    slug: str
    # The full name, shown in the switcher and in every empty state. `us` is not
    # a label a person reads, and an unused region looks exactly like a wiped one
    # unless the screen says which region it is empty in.
    name: str
    # Where this region's API lives. The dashboard builds its client from this,
    # which is what makes one static build serve every region.
    api_url: str
    # Exactly one region carries this: where a workspace opens when nothing else
    # decides.
    default: bool


class RegionsResponse(BaseModel):
    regions: list[RegionResponse]


@router.get("", response_model=RegionsResponse)
async def list_regions() -> RegionsResponse:
    """Every region this deployment serves, and where each one's API is.

    Configuration, not a table — the list changes when a region is stood up,
    which is a deploy.
    """
    return RegionsResponse(
        regions=[
            RegionResponse(
                slug=region.slug,
                name=region.name,
                api_url=region.api_url,
                default=region.default,
            )
            for region in get_settings().regions
        ]
    )
