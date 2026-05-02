"""Datetime computations for view models (brief task 6.2).

Tiny helpers used by `agent_state_service` view-builders to surface
"days/minutes until X" + threshold flags. Pure functions, no DB.

All timestamps are treated as naive UTC (project convention; see
`PROJECT_BRIEF.md` §7 "Datetime: SEMPRE UTC"). The helpers don't try to
interpret tz-aware vs naive — they assume the caller normalized.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Final


_SECONDS_PER_DAY: Final[int] = 86_400
_SECONDS_PER_MINUTE: Final[int] = 60


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_today() -> date:
    """Current UTC date — explicit, avoids local-TZ ambiguity.

    `date.today()` returns the local-TZ date, which on a non-UTC host
    rolls at local midnight rather than UTC midnight. Cost-tracking
    and cap-reset windows are anchored to UTC, so any "today" lookup
    that gates spend MUST go through this helper.

    Pattern lock: see [7.2.5] postmortem on naive datetime
    `.timestamp()` for the symmetric bug on the time side.
    """
    return datetime.now(timezone.utc).date()


def days_until(target: datetime, *, now: datetime | None = None) -> int:
    """Whole days until `target`. Returns 0 if already past.

    `now` injectable for tests; default is naive UTC now.
    """
    delta = target - (now or _utcnow())
    secs = delta.total_seconds()
    if secs <= 0:
        return 0
    return int(secs / _SECONDS_PER_DAY)


def minutes_until(target: datetime, *, now: datetime | None = None) -> int:
    """Whole minutes until `target`. Returns 0 if already past."""
    delta = target - (now or _utcnow())
    secs = delta.total_seconds()
    if secs <= 0:
        return 0
    return int(secs / _SECONDS_PER_MINUTE)


def is_near_cap(used: float, total: float, threshold: float = 0.8) -> bool:
    """True iff `used > threshold * total`. False on `total <= 0` (defensive)."""
    if total <= 0:
        return False
    return used / total > threshold
