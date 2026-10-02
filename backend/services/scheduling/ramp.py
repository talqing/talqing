"""The daily limit: a fixed number, or one that ramps up as the list is worked.

A ramp advances on **sending days** — local days on which the batch sent at
least once — never on the calendar. A closed window, a weekend or a pause earns
nothing, so a ramp that reached 14 on Friday resumes at 15 on Monday, and one
paused for a week resumes at the level it stopped at.

Today is never counted, so a limit cannot change mid-day: the batch keeps two
facts (`days`, the sending days so far, and `last_day`, the latest of them) and
the ramp reads only the days already *completed*. Each row that stores a ramp
also stores `base_days`, the completed days at the moment the ramp was set —
which is what makes "restart" one write and lets a copy of a ramp carry its
progress with it.

`daily_cap_today` is the one implementation of the formula. Every claim, every
"is today spent" check and every response calls it.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FixedDailyCap(BaseModel):
    """The same limit every day."""

    kind: Literal["fixed"]
    limit: int = Field(ge=1)

    model_config = ConfigDict(extra="forbid")


class RampDailyCap(BaseModel):
    """`start` a day, raised by `step` every `interval_days` sending days, up to `end`."""

    kind: Literal["ramp"]
    start: int = Field(ge=1)
    end: int
    step: int = Field(ge=1)
    interval_days: int = Field(ge=1, le=30)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _rises(self) -> RampDailyCap:
        if self.end <= self.start:
            raise ValueError("a ramp's end must be greater than its start")
        return self


DailyCap = Annotated[FixedDailyCap | RampDailyCap, Field(discriminator="kind")]

# The five stored columns, in order: the fixed limit, then the ramp's four.
CapColumns = tuple[int | None, int | None, int | None, int | None, int | None]


def cap_columns(cap: FixedDailyCap | RampDailyCap | None) -> CapColumns:
    """A daily cap as its stored columns: `(limit, start, end, step, interval_days)`."""
    if cap is None:
        return None, None, None, None, None
    if isinstance(cap, FixedDailyCap):
        return cap.limit, None, None, None, None
    return None, cap.start, cap.end, cap.step, cap.interval_days


def cap_from_columns(
    limit: int | None,
    start: int | None,
    end: int | None,
    step: int | None,
    interval_days: int | None,
) -> FixedDailyCap | RampDailyCap | None:
    """The stored columns as a daily cap. The table's CHECKs keep them coherent."""
    if start is not None:
        assert end is not None and step is not None and interval_days is not None
        return RampDailyCap(
            kind="ramp", start=start, end=end, step=step, interval_days=interval_days
        )
    return None if limit is None else FixedDailyCap(kind="fixed", limit=limit)


class StoredSendCap(BaseModel):
    """The `send_*` cap columns of a row, and the one shape they render as."""

    send_daily_cap: int | None
    send_ramp_start: int | None
    send_ramp_end: int | None
    send_ramp_step: int | None
    send_ramp_interval_days: int | None
    send_ramp_base_days: int

    @property
    def daily_cap(self) -> FixedDailyCap | RampDailyCap | None:
        return cap_from_columns(
            self.send_daily_cap,
            self.send_ramp_start,
            self.send_ramp_end,
            self.send_ramp_step,
            self.send_ramp_interval_days,
        )


def completed_days(days: int, last_day: date | None, today: date) -> int:
    """Sending days that are over: today, if it has sent, is not one yet."""
    return days - (1 if last_day == today else 0)


def ramp_base_days(
    new: FixedDailyCap | RampDailyCap | None,
    old: FixedDailyCap | RampDailyCap | None,
    *,
    old_base: int,
    completed: int,
) -> int:
    """The baseline to store beside `new`: its progress kept, or restarted.

    A ramp keeps its progress through an edit unless its `start` changed or
    there was no ramp before; those two restart it at `start`, from today.
    """
    if not isinstance(new, RampDailyCap):
        return 0
    if isinstance(old, RampDailyCap) and old.start == new.start:
        return old_base
    return completed


def daily_cap_today(
    cap: FixedDailyCap | RampDailyCap | None,
    *,
    base_days: int,
    days: int,
    last_day: date | None,
    today: date,
) -> int | None:
    """Today's limit: the fixed one, the ramp's value today, or None when uncapped."""
    if cap is None:
        return None
    if isinstance(cap, FixedDailyCap):
        return cap.limit
    ramp_days = max(0, completed_days(days, last_day, today) - base_days)
    return min(cap.end, cap.start + cap.step * (ramp_days // cap.interval_days))
