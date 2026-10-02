"""FAQs HTTP adapter over services.faqs.

Every member can read FAQs; editors write them. Entries are written one at a
time or appended in bulk rather than replaced as a list, so fixing one answer
never resends the other 499 and two editors cannot overwrite each other.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query

from api.core.schemas import OkResponse, Page
from api.dataplane.deps import Context, CtxDep, WriteCtxDep
from services import faqs as svc
from services.faqs import (
    CreateFaqEntriesRequest,
    FaqDefinition,
    FaqDetail,
    FaqEntriesResponse,
    FaqEntry,
    FaqSummary,
    UpdateFaqEntryRequest,
    UpdateFaqRequest,
)

router = APIRouter(prefix="/faqs", tags=["faqs"])


@router.get("", response_model=Page[FaqSummary])
async def list_faqs(
    ctx: Context = CtxDep,
    limit: int = Query(200, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[FaqSummary]:
    """List the workspace's FAQs by name, with how many questions each holds."""
    return await svc.list_faqs(ctx, limit, offset)


@router.post("", status_code=201, response_model=FaqDetail)
async def create_faq(body: FaqDefinition, ctx: Context = WriteCtxDep) -> FaqDetail:
    """Create an FAQ, optionally with its first question/answer entries."""
    return await svc.create_faq(body, ctx)


@router.get("/{faq_id}", response_model=FaqDetail)
async def get_faq(faq_id: UUID, ctx: Context = CtxDep) -> FaqDetail:
    """Get one FAQ with every entry, in the order they were added."""
    return await svc.get_faq(faq_id, ctx)


@router.patch("/{faq_id}", response_model=FaqSummary)
async def update_faq(faq_id: UUID, body: UpdateFaqRequest, ctx: Context = WriteCtxDep):
    """Rename an FAQ."""
    return await svc.update_faq(faq_id, body, ctx)


@router.delete("/{faq_id}", response_model=OkResponse)
async def delete_faq(faq_id: UUID, ctx: Context = WriteCtxDep):
    """Delete an FAQ and its entries. Refused with 409 while an agent or task
    still attaches it."""
    return await svc.delete_faq(faq_id, ctx)


@router.post("/{faq_id}/entries", status_code=201, response_model=FaqEntriesResponse)
async def create_faq_entries(
    faq_id: UUID, body: CreateFaqEntriesRequest, ctx: Context = WriteCtxDep
) -> FaqEntriesResponse:
    """Append question/answer entries to an FAQ, all or nothing. Live at once:
    agents using the FAQ read them from their next session."""
    return await svc.create_faq_entries(faq_id, body, ctx)


@router.patch("/{faq_id}/entries/{entry_id}", response_model=FaqEntry)
async def update_faq_entry(
    faq_id: UUID, entry_id: UUID, body: UpdateFaqEntryRequest, ctx: Context = WriteCtxDep
) -> FaqEntry:
    """Change one entry's question, answer or both. Live from the next session."""
    return await svc.update_faq_entry(faq_id, entry_id, body, ctx)


@router.delete("/{faq_id}/entries/{entry_id}", response_model=OkResponse)
async def delete_faq_entry(faq_id: UUID, entry_id: UUID, ctx: Context = WriteCtxDep):
    """Delete one entry from an FAQ."""
    return await svc.delete_faq_entry(faq_id, entry_id, ctx)
