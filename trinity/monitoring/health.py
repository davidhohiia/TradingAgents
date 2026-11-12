"""Health evaluation of bots, venues and the database."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from trinity.database.models import str_to_ts
from trinity.database.repository import Repository


@dataclass
class HealthReport:
    ok: bool = True
    problems: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def problem(self, msg: str) -> None:
        self.ok = False
        self.problems.append(msg)


def check_health(repo: Repository, now: datetime, intervals: dict[str, float],
                 breakers: dict[str, dict[str, Any]] | None = None, stale_factor: float = 5.0) -> HealthReport:
    rep = HealthReport()
    if not repo.integrity_ok():
        rep.problem("database integrity check failed")
    for row in repo.list_bot_health():
        sid = row["strategy_id"]
        hb = str_to_ts(row["last_heartbeat"])
        interval = intervals.get(sid, 60)
        rep.details[sid] = {"status": row["status"], "last_heartbeat": row["last_heartbeat"]}
        if row["status"] == "STOPPED":
            continue
        if hb is None or now - hb > timedelta(seconds=max(interval * stale_factor, 120)):
            rep.problem(f"{sid}: no heartbeat since {row['last_heartbeat']}")
        if row["status"] in ("CONNECTION ERROR", "ERROR"):
            rep.problem(f"{sid}: {row['status']} {row['last_error'] or ''}".strip())
    for venue, b in (breakers or {}).items():
        if b.get("state") != "closed":
            rep.problem(f"venue {venue}: circuit breaker {b.get('state')} ({b.get('last_error')})")
    return rep
