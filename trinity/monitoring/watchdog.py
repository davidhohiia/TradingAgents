"""Watchdog: detects stalled bot loops / scheduler tasks, raises alerts, records metrics.

It never re-enables trading after a risk lock and never switches paper -> live.
External restarts of the whole process (after a crash / reboot) are handled by the
OS task scheduler (see DEPLOYMENT_WINDOWS.md); such restarts always start in PAPER mode.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from trinity.core.log import get_logger
from trinity.core.portfolio_manager import PortfolioManager
from trinity.monitoring import system_metrics
from trinity.monitoring.health import check_health

log = get_logger(__name__)


class Watchdog:
    def __init__(self, pm: PortfolioManager, data_dir, alert_cooldown: timedelta = timedelta(minutes=30)) -> None:  # noqa: ANN001
        self.pm = pm
        self.data_dir = data_dir
        self.alert_cooldown = alert_cooldown
        self._last_alert: dict[str, datetime] = {}

    async def check(self) -> list[str]:
        now = self.pm.clock.now()
        problems: list[str] = []
        for name, task in self.pm.scheduler.tasks.items():
            if not task.enabled:
                continue
            if task.running and task.last_run and now - task.last_run > timedelta(seconds=task.timeout * 2):
                problems.append(f"task {name} appears stuck")
            if task.consecutive_failures >= 5:
                problems.append(f"task {name} failing repeatedly: {task.last_error}")
        intervals = {sid: b.loop_interval for sid, b in self.pm.bots.items()}
        health = check_health(self.pm.repo, now, intervals, self.pm.engine.breakers.snapshot())
        problems.extend(health.problems)
        try:
            metrics = system_metrics.collect(self.data_dir)
            problems.extend(system_metrics.warnings(metrics))
        except Exception as exc:  # noqa: BLE001
            log.debug("metrics unavailable: %s", exc)
        for p in problems:
            last = self._last_alert.get(p)
            if last is None or now - last > self.alert_cooldown:
                self._last_alert[p] = now
                self.pm.repo.add_system_event("WARNING", "watchdog", "problem", p, ts=now)
                await self.pm.notifier.notify("watchdog", f"Watchdog: {p}", "WARNING")
        return problems
