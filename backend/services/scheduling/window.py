"""Business hours: the one place a scheduled list's clock is read.

Evaluated **once per batch per pass** — never per row. "May this batch run right
now?" is a single boolean asked before the work table is touched at all, which is
what keeps the window off the claim query and out of SQL entirely. SQL never does
timezone arithmetic here.

The same function produces the next opening, so "we are not running because it is
19:00 on a Friday" and "we will start again at 10:00 on Monday" are two reads of
one rule rather than two implementations that agree today.

Arguments are explicit rather than a batch row, because three different rows —
telephony's ``call_batches``, email's ``email_batches`` and its
``email_send_runs`` — carry the same four columns under the same names and must
not each get their own copy of this arithmetic. ``TimeWindow`` below is the API
shape those three share for the same reason.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator


class TimeWindow(BaseModel):
    """The hours a list may run in, on the list's own clock.

    Shared rather than duplicated, for the same reason ``window_state`` is: a
    call batch and an email send run store the same three values under the same
    names and must not each get their own copy of the rules. The field that
    carries one is named for what it gates — ``calling_window`` on a campaign,
    ``window`` on an email batch and its runs.

    A window that crosses midnight (``21:00``-``06:00``) is valid and means
    exactly what an evening consumer-calling window means: the day check applies
    to the *start* side, so ``21:00``-``06:00`` on ``days: [5]`` runs Friday
    21:00 through Saturday 06:00.
    """

    start: time
    end: time
    days: list[int] = Field(
        default=[1, 2, 3, 4, 5, 6, 7],
        description="ISO weekdays the window opens on, 1 = Monday.",
    )

    @field_validator("days")
    @classmethod
    def _iso_weekdays(cls, value: list[int]) -> list[int]:
        days = sorted(set(value))
        if not days:
            raise ValueError("a window must name at least one day")
        if any(d < 1 or d > 7 for d in days):
            raise ValueError("window days are ISO weekdays, 1 (Monday) to 7 (Sunday)")
        return days

    @field_validator("end")
    @classmethod
    def _non_empty(cls, value: time, info: Any) -> time:
        # A zero-length window can never open: nothing would ever be claimable
        # and the list would live forever with nothing to do. Refused here
        # rather than survived at runtime.
        if info.data.get("start") == value:
            raise ValueError("a window's start and end must differ")
        return value


# A non-empty day set and a non-zero-length window always open within a week —
# both are constraints on every table that stores one, so this bound is
# reachable only if one of them was subverted, and running past it would be an
# infinite loop.
_MAX_DAYS_AHEAD = 8


def local_day_bounds(now: datetime, timezone: str) -> tuple[datetime, datetime]:
    """``(this local day's midnight, the next one)``, as aware instants.

    The one place a "per day" ceiling turns into two timestamps, so SQL never
    does timezone arithmetic and a daily cap means the day the LIST is worked in
    rather than the day the server happens to be having. Both bounds come back
    aware, so the caller compares them against a stored `timestamptz` directly.
    """
    tz = ZoneInfo(timezone)
    local = now.astimezone(tz)
    start = datetime.combine(local.date(), time(0, 0), tzinfo=tz)
    return start, datetime.combine(local.date() + timedelta(days=1), time(0, 0), tzinfo=tz)


def local_today(now: datetime, timezone: str) -> date:
    """The calendar day `now` falls on in `timezone` — the day a daily limit counts."""
    return now.astimezone(ZoneInfo(timezone)).date()


def window_state(
    *,
    now: datetime,
    timezone: str,
    window_start_local: time | None,
    window_end_local: time | None,
    window_days: list[int],
    subject: str,
) -> tuple[bool, datetime | None]:
    """``(may_run_now, when_it_next_opens)``.

    The second value is what the UI renders as "waiting until Monday 10:00"; it
    is ``None`` while the window is open, because a batch that can run is not
    waiting for anything.

    ``now`` is an aware instant. The server runs UTC and the batch does not, so
    everything below happens after converting into ``timezone`` — a
    ``datetime.now()`` without a zone silently passes in India and fails
    everywhere else.

    ``subject`` names the row in the impossible-window error, so an operator
    reading the log knows which batch to fix.
    """
    if window_start_local is None or window_end_local is None:
        return True, None

    tz = ZoneInfo(timezone)
    local = now.astimezone(tz)
    start = window_start_local
    end = window_end_local
    days = set(window_days)

    if start < end:
        # Ordinary same-day window: 10:00–18:00 on the days named.
        open_now = local.isoweekday() in days and start <= local.time() < end
    else:
        # Crosses midnight: the window that OPENS on day D runs to `end` on
        # D+1, so the day check belongs to the start side. 21:00–06:00 on
        # days=[5] is Friday 21:00 through Saturday 06:00 — Saturday evening is
        # closed, and the naive `start <= t <= end` gets both halves wrong.
        open_now = (local.isoweekday() in days and local.time() >= start) or (
            (local - timedelta(days=1)).isoweekday() in days and local.time() < end
        )
    if open_now:
        return True, None

    for offset in range(_MAX_DAYS_AHEAD):
        day = (local + timedelta(days=offset)).date()
        if day.isoweekday() not in days:
            continue
        opening = datetime.combine(day, start, tzinfo=tz)
        if opening > local:
            return False, opening
    # Unreachable while the table's CHECK constraints hold. Loud rather than a
    # batch that quietly never runs again.
    raise RuntimeError(
        f"the calling window for {subject} never opens "
        f"({start}–{end} on ISO days {sorted(days)} in {timezone})"
    )
