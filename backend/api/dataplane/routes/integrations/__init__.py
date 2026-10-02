"""Tenant integration routes."""

from __future__ import annotations

from fastapi import APIRouter

from . import core, oauth, provider_webhooks, triggers

router = APIRouter()
router.include_router(oauth.router, prefix="/integrations", tags=["integrations"])
router.include_router(provider_webhooks.router, prefix="/integrations", tags=["integrations"])
router.include_router(triggers.router, prefix="/integrations", tags=["integrations"])
router.include_router(core.router, prefix="/integrations", tags=["integrations"])
