"""Carrier-specific telephony adapters."""

from __future__ import annotations

from .base import (
    OutboundInlineConfig,
    ProviderError,
    RemoteNumberInfo,
    TelephonyProviderAdapter,
    get_adapter,
)
from .exotel import (
    CHECKLIST_CONSOLE_SETUP,
    ExotelAdapter,
    exotel_inbound_console_ready,
    set_exotel_console_setup,
)
from .plivo import PlivoAdapter
from .twilio import TwilioAdapter
from .vobiz import VobizAdapter

__all__ = [
    "CHECKLIST_CONSOLE_SETUP",
    "ExotelAdapter",
    "OutboundInlineConfig",
    "PlivoAdapter",
    "ProviderError",
    "RemoteNumberInfo",
    "TelephonyProviderAdapter",
    "TwilioAdapter",
    "VobizAdapter",
    "exotel_inbound_console_ready",
    "get_adapter",
    "set_exotel_console_setup",
]
