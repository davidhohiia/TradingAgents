"""Exchange / broker API monitoring: call counts, error rate, latency percentiles.

``ApiMonitor.wrap(obj, venue)`` returns a transparent proxy that times every async
method call of a venue/data source. Aggregates are flushed to the ``api_health`` table.
"""

from __future__ import annotations

import inspect
import statistics
import time
from collections import defaultdict, deque
from datetime import datetime
from typing import Any

from trinity.database.repository import Repository


class ApiMonitor:
    def __init__(self, window: int = 500) -> None:
        self.calls: dict[str, deque[tuple[float, bool]]] = defaultdict(lambda: deque(maxlen=window))
        self.last_error: dict[str, str] = {}
        self.totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])

    def record(self, venue: str, latency_ms: float, ok: bool, error: str | None = None) -> None:
        self.calls[venue].append((latency_ms, ok))
        t = self.totals[venue]
        t[0] += 1
        if not ok:
            t[1] += 1
            self.last_error[venue] = (error or "")[:300]

    def stats(self, venue: str) -> dict[str, Any]:
        data = list(self.calls[venue])
        lats = sorted(x[0] for x in data)
        errors = sum(1 for _, ok in data if not ok)
        return {
            "calls": len(data), "errors": errors, "error_rate": errors / len(data) if data else 0.0,
            "p50_ms": statistics.median(lats) if lats else None,
            "p95_ms": lats[int(0.95 * (len(lats) - 1))] if lats else None,
            "last_error": self.last_error.get(venue),
        }

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {v: self.stats(v) for v in self.calls}

    def problems(self, max_error_rate: float = 0.2, max_p95_ms: float = 5000) -> list[str]:
        out = []
        for v, s in self.snapshot().items():
            if s["calls"] >= 10 and s["error_rate"] > max_error_rate:
                out.append(f"API {v}: error rate {s['error_rate']:.0%} ({s['last_error']})")
            if s["p95_ms"] is not None and s["p95_ms"] > max_p95_ms:
                out.append(f"API {v}: slow responses (p95 {s['p95_ms']:.0f} ms)")
        return out

    def flush(self, repo: Repository, now: datetime) -> None:
        for v, s in self.snapshot().items():
            repo.add_api_health(now, v, s["calls"], s["errors"], s["p50_ms"], s["p95_ms"], s["last_error"])

    def wrap(self, target: Any, venue: str) -> Any:
        return MonitoredProxy(target, venue, self)


class MonitoredProxy:
    """Transparent proxy: attribute access passes through, async methods are timed."""

    def __init__(self, target: Any, venue: str, monitor: ApiMonitor) -> None:
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_venue", venue)
        object.__setattr__(self, "_monitor", monitor)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._target, name)
        if name.startswith("_") or not inspect.iscoroutinefunction(attr):
            return attr
        monitor, venue = self._monitor, self._venue

        async def timed(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            try:
                result = await attr(*args, **kwargs)
            except Exception as exc:
                monitor.record(venue, (time.perf_counter() - start) * 1000, False, f"{type(exc).__name__}: {exc}")
                raise
            monitor.record(venue, (time.perf_counter() - start) * 1000, True)
            return result

        return timed

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._target, name, value)

    @property
    def unwrapped(self) -> Any:
        return self._target
