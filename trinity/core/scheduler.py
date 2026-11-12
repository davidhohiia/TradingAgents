"""Async periodic scheduler with error isolation and per-task timeouts.

The same scheduler drives real time (``run_forever``) and simulated time (``run_pending``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from trinity.core.clock import Clock
from trinity.core.log import get_logger

log = get_logger(__name__)


@dataclass
class PeriodicTask:
    name: str
    interval: float
    fn: Callable[[], Awaitable[object]]
    timeout: float = 120.0
    next_run: datetime | None = None
    running: bool = False
    runs: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    last_error: str | None = None
    last_run: datetime | None = None
    last_duration: float = 0.0
    enabled: bool = True
    tags: set[str] = field(default_factory=set)


class Scheduler:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.tasks: dict[str, PeriodicTask] = {}
        self._stop = asyncio.Event()
        self.on_failure: Callable[[PeriodicTask, BaseException], Awaitable[None]] | None = None

    def add(self, name: str, interval: float, fn: Callable[[], Awaitable[object]], *, timeout: float = 120.0,
            run_immediately: bool = True) -> PeriodicTask:
        t = PeriodicTask(name, interval, fn, timeout)
        now = self.clock.now()
        t.next_run = now if run_immediately else now + timedelta(seconds=interval)
        self.tasks[name] = t
        return t

    def remove(self, name: str) -> None:
        self.tasks.pop(name, None)

    async def _run(self, t: PeriodicTask) -> None:
        t.running = True
        start = self.clock.now()
        try:
            await asyncio.wait_for(t.fn(), timeout=t.timeout)
            t.consecutive_failures = 0
            t.last_error = None
        except Exception as exc:  # noqa: BLE001 - isolate task failures
            t.failures += 1
            t.consecutive_failures += 1
            t.last_error = f"{type(exc).__name__}: {exc}"
            log.exception("scheduled task %s failed", t.name)
            if self.on_failure is not None:
                try:
                    await self.on_failure(t, exc)
                except Exception:  # noqa: BLE001
                    log.exception("on_failure handler failed")
        finally:
            t.runs += 1
            t.running = False
            t.last_run = start
            t.last_duration = (self.clock.now() - start).total_seconds()
            backoff = min(t.interval * (2 ** min(t.consecutive_failures, 5)), max(t.interval, 900))
            delay = backoff if t.consecutive_failures else t.interval
            t.next_run = start + timedelta(seconds=delay)

    async def run_pending(self) -> int:
        now = self.clock.now()
        due = [t for t in self.tasks.values()
               if t.enabled and not t.running and t.next_run is not None and t.next_run <= now]
        if due:
            await asyncio.gather(*(self._run(t) for t in due))
        return len(due)

    async def run_forever(self, tick: float = 1.0) -> None:
        while not self._stop.is_set():
            await self.run_pending()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=tick)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
