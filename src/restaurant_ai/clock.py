"""A single source of "now".

Agents are scheduled around wall-clock time, and the simulator needs to replay a
service day at arbitrary speed. Everything therefore reads the time through this
module rather than calling ``datetime.now()``, so the simulator can move time
without any agent knowing.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from restaurant_ai.config import get_settings

_frozen_at: datetime | None = None


def local_tz() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


def now() -> datetime:
    """Current local time, honouring any active freeze."""
    if _frozen_at is not None:
        return _frozen_at
    return datetime.now(tz=local_tz())


def utcnow() -> datetime:
    return now().astimezone(UTC)


def today() -> date:
    return now().date()


def start_of_day(day: date | None = None) -> datetime:
    day = day or today()
    return datetime.combine(day, datetime.min.time(), tzinfo=local_tz())


def end_of_day(day: date | None = None) -> datetime:
    return start_of_day(day) + timedelta(days=1) - timedelta(microseconds=1)


@contextlib.contextmanager
def freeze(at: datetime) -> Iterator[datetime]:
    """Pin `now()` for the duration of the block. Used by the simulator and tests."""
    global _frozen_at
    previous = _frozen_at
    if at.tzinfo is None:
        at = at.replace(tzinfo=local_tz())
    _frozen_at = at
    try:
        yield at
    finally:
        _frozen_at = previous


def set_frozen(at: datetime | None) -> None:
    """Non-context-manager variant, for long-running simulated days."""
    global _frozen_at
    if at is not None and at.tzinfo is None:
        at = at.replace(tzinfo=local_tz())
    _frozen_at = at
