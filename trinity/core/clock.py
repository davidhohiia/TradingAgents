"""Clock abstraction so the whole system can run on simulated time (tests, 24h simulation)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class RealClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class SimClock:
    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> datetime:
        self._now = self._now + timedelta(seconds=seconds)
        return self._now

    def set(self, ts: datetime) -> None:
        self._now = ts


def utc_day(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%d")
