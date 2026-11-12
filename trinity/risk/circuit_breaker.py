"""Circuit breaker for external APIs (exchanges, brokers, data providers)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from trinity.core.clock import Clock, RealClock


class BreakerState(StrEnum):
    CLOSED = "closed"  # normal
    OPEN = "open"  # failing: calls blocked
    HALF_OPEN = "half_open"  # cooldown elapsed: one probe allowed


@dataclass
class CircuitBreaker:
    name: str
    failure_threshold: int = 5
    cooldown_seconds: float = 120
    clock: Clock = field(default_factory=RealClock)
    consecutive_failures: int = 0
    opened_at: datetime | None = None
    last_error: str | None = None
    total_failures: int = 0

    @property
    def state(self) -> BreakerState:
        if self.opened_at is None:
            return BreakerState.CLOSED
        if self.clock.now() - self.opened_at >= timedelta(seconds=self.cooldown_seconds):
            return BreakerState.HALF_OPEN
        return BreakerState.OPEN

    def allow(self) -> bool:
        return self.state != BreakerState.OPEN

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.opened_at = None
        self.last_error = None

    def record_failure(self, error: str) -> bool:
        """Returns True if this failure opened (or re-opened) the breaker."""
        self.consecutive_failures += 1
        self.total_failures += 1
        self.last_error = error[:500]
        was_half_open = self.state == BreakerState.HALF_OPEN
        if self.consecutive_failures >= self.failure_threshold or was_half_open:
            self.opened_at = self.clock.now()
            return True
        return False


class CircuitBreakerRegistry:
    def __init__(self, failure_threshold: int, cooldown_seconds: float, clock: Clock | None = None) -> None:
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.clock = clock or RealClock()
        self._breakers: dict[str, CircuitBreaker] = {}

    def get(self, name: str) -> CircuitBreaker:
        if name not in self._breakers:
            self._breakers[name] = CircuitBreaker(
                name, self.failure_threshold, self.cooldown_seconds, self.clock
            )
        return self._breakers[name]

    def allow(self, name: str) -> bool:
        return self.get(name).allow()

    def snapshot(self) -> dict[str, dict[str, object]]:
        return {
            n: {"state": b.state.value, "failures": b.consecutive_failures, "last_error": b.last_error}
            for n, b in self._breakers.items()
        }
