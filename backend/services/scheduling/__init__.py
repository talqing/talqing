"""Scheduling primitives shared by everything that runs a list on a clock.

Import what you need from here::

    from services.scheduling import TimeWindow, local_day_bounds, window_state

Batch outbound calling and email outbound both work through a business-hours
window on the list's own timezone, and both ask the same two questions of it:
may this run right now, and when does it next open. One implementation, so a
change to the midnight-crossing rule cannot reach one caller and miss the other.
"""

from __future__ import annotations

from .ramp import (  # noqa: F401
    CapColumns,
    DailyCap,
    FixedDailyCap,
    RampDailyCap,
    StoredSendCap,
    cap_columns,
    cap_from_columns,
    completed_days,
    daily_cap_today,
    ramp_base_days,
)
from .window import TimeWindow, local_day_bounds, local_today, window_state  # noqa: F401

__all__ = [
    "CapColumns",
    "DailyCap",
    "FixedDailyCap",
    "RampDailyCap",
    "StoredSendCap",
    "TimeWindow",
    "cap_columns",
    "cap_from_columns",
    "completed_days",
    "daily_cap_today",
    "local_day_bounds",
    "local_today",
    "ramp_base_days",
    "window_state",
]
