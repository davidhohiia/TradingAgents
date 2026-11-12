"""Central Portfolio Manager.

Responsibilities: bot lifecycle (start/stop/pause, supervised restarts), capital
allocation overview, exposure and position overview, equity / PnL tracking, risk
coordination (daily-loss and drawdown reactions), daily reports, operator control
API used by Telegram and scripts.

Bots never talk to venues directly for trading: they hold an OrderGateway that routes
everything through the ExecutionEngine and the Global Risk Manager.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from trinity.ai.performance_analyzer import PerformanceAnalyzer
from trinity.bots.base import BaseBot
from trinity.config.settings import AppConfig
from trinity.core.capital_allocator import CapitalAllocator
from trinity.core.clock import Clock, RealClock, utc_day
from trinity.core.execution_engine import ExecutionEngine
from trinity.core.log import get_logger
from trinity.core.models import Mode, Side
from trinity.core.scheduler import PeriodicTask, Scheduler
from trinity.database.repository import Repository
from trinity.exchanges.base_exchange import ExchangeError
from trinity.notifications import message_templates as tpl
from trinity.notifications.notifier import LogNotifier, Notifier

log = get_logger(__name__)

MAX_BOT_RESTARTS = 10
EQUITY_SNAPSHOT_EVERY = timedelta(minutes=5)


class PortfolioManager:
    def __init__(
        self,
        *,
        config: AppConfig,
        repo: Repository,
        engine: ExecutionEngine,
        mode: Mode,
        bots: list[BaseBot],
        clock: Clock | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        self.config = config
        self.repo = repo
        self.engine = engine
        self.risk = engine.risk
        self.allocator: CapitalAllocator = engine.allocator
        self.positions = engine.positions
        self.mode = mode
        self.bots = {b.strategy_id: b for b in bots}
        self.clock = clock or RealClock()
        self.notifier = notifier or LogNotifier()
        self.scheduler = Scheduler(self.clock)
        self.scheduler.on_failure = self._on_task_failure
        self.performance = PerformanceAnalyzer(repo)
        self.restarts: dict[str, int] = dict.fromkeys(self.bots, 0)
        self._last_equity_snapshot: datetime | None = None
        self._last_report_day: str | None = repo.get_kv(f"pm:{mode.value}:last_report_day")
        self.last_equity: float | None = None
        self.started_at: datetime | None = None
        engine.on_event(self._on_engine_event)

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self.started_at = self.clock.now()
        for sid, bot in self.bots.items():
            self.repo.upsert_strategy(sid, sid.replace("_", " ").title(), bot.bot_type,
                                      self.config.allocation(sid), True)
        for venue in self.engine.venues.values():
            self.repo.upsert_account(venue.name, self.mode, "USD", f"{venue.name} ({self.mode.value})")
        for bot in self.bots.values():
            await bot.start()
            self.scheduler.add(f"bot:{bot.strategy_id}", bot.loop_interval, self._bot_runner(bot))
        self.scheduler.add("sync_orders", 15, self.engine.sync_orders, timeout=120)
        self.scheduler.add("risk_monitor", 30, self.monitor, timeout=60)
        self.scheduler.add("daily_report", 300, self.maybe_daily_report, timeout=60)
        self.repo.add_system_event("INFO", "portfolio", "start", f"started in {self.mode.value} mode",
                                   {"bots": list(self.bots)}, ts=self.clock.now())
        await self.notifier.notify("system", f"TRINITY started. {tpl.MODE_BANNER[self.mode.value]}")

    def _bot_runner(self, bot: BaseBot):  # noqa: ANN202
        async def run() -> None:
            ok = await bot.safe_step()
            if not ok and bot.consecutive_errors in (3, 20):
                await self.notifier.notify("api_error", f"{bot.strategy_id}: connection problem: {bot.last_error}",
                                           "WARNING")
        return run

    async def _on_task_failure(self, task: PeriodicTask, exc: BaseException) -> None:
        if not task.name.startswith("bot:"):
            self.repo.add_system_event("ERROR", "scheduler", "task_failed", f"{task.name}: {exc}", ts=self.clock.now())
            return
        sid = task.name.split(":", 1)[1]
        self.restarts[sid] = self.restarts.get(sid, 0) + 1
        bot = self.bots[sid]
        bot.last_error = f"{type(exc).__name__}: {exc}"
        self.repo.upsert_bot_health(sid, "ERROR", self.mode, bot.last_error, self.restarts[sid], ts=self.clock.now())
        if self.restarts[sid] >= MAX_BOT_RESTARTS:
            task.enabled = False
            await bot.stop()
            await self.engine.cancel_where(strategy_id=sid, side=Side.BUY, reason="bot stopped after repeated errors")
            await self.notifier.notify("bot_stopped", f"{sid} stopped after {self.restarts[sid]} failures: {exc}",
                                       "CRITICAL")
        else:
            log.warning("bot %s failed (%d), will retry with backoff", sid, self.restarts[sid])

    async def stop(self, cancel_orders: bool = False) -> None:
        self.scheduler.stop()
        for bot in self.bots.values():
            await bot.stop()
        if cancel_orders:
            await self.engine.cancel_where(reason="system stop")
        self.repo.add_system_event("INFO", "portfolio", "stop", "stopped", ts=self.clock.now())

    async def tick(self) -> int:
        """Run all due tasks once (simulation / tests)."""
        return await self.scheduler.run_pending()

    # ------------------------------------------------------------------ engine events
    async def _on_engine_event(self, kind: str, payload: dict[str, Any]) -> None:
        if kind == "api_error":
            await self.notifier.notify("api_error", f"API problem on {payload['venue']}: {payload['error']}", "WARNING")

    # ------------------------------------------------------------------ equity & risk
    async def mark_to_market(self) -> None:
        symbols: dict[tuple[str, str], None] = {}
        for p in self.positions.all(open_only=True):
            symbols[(p.venue, p.symbol)] = None
        for venue_name, symbol in symbols:
            venue = self.engine.venues.get(venue_name)
            if venue is None:
                continue
            try:
                t = await venue.fetch_ticker(symbol)
            except ExchangeError as exc:
                log.warning("mark-to-market failed for %s: %s", symbol, exc)
                continue
            price = t.bid or t.last  # conservative: liquidation side
            if price:
                self.positions.mark_to_market(symbol, price, self.clock.now())

    def equity_breakdown(self) -> dict[str, float]:
        t = self.positions.totals()
        equity = self.config.capital.total_capital_usd + t["realized"] + t["unrealized"]
        return {
            "equity": equity, "realized": t["realized"], "unrealized": t["unrealized"],
            "invested": t["invested"], "market_value": t["market_value"],
            "cash": equity - t["market_value"],
        }

    async def monitor(self) -> None:
        await self.mark_to_market()
        eq = self.equity_breakdown()
        self.last_equity = eq["equity"]
        actions = self.risk.on_equity(eq["equity"])
        for a in actions:
            if a.kind == "DAILY_LOCK":
                n = await self.engine.cancel_where(side=Side.BUY, reason="daily loss limit")
                await self.notifier.notify("risk_limit", f"⛔ {a.message}. Canceled {n} buy orders. "
                                           "Positions kept; new entries blocked until next UTC day.", "CRITICAL")
            elif a.kind == "DRAWDOWN_LOCK":
                n = await self.engine.cancel_where(reason="max drawdown lock")
                await self.notifier.notify("risk_limit", f"🛑 {a.message} Canceled {n} open orders. Positions were NOT "
                                           "liquidated. Run scripts/emergency_stop.py --confirm-drawdown after review.",
                                           "CRITICAL")
            elif a.kind == "DRAWDOWN_WARNING":
                await self.notifier.notify("drawdown_warning", f"⚠️ {a.message}", "WARNING")
        now = self.clock.now()
        st = self.risk.last_status
        if self._last_equity_snapshot is None or now - self._last_equity_snapshot >= EQUITY_SNAPSHOT_EVERY:
            self._last_equity_snapshot = now
            self.repo.add_equity_snapshot(now, self.mode, eq["equity"], eq["cash"], eq["invested"], eq["realized"],
                                          eq["unrealized"], st.drawdown_pct if st else 0.0)
            if st:
                self.repo.upsert_daily_metrics(st.day, self.mode, "ALL", start_equity=st.day_start_equity,
                                               end_equity=eq["equity"], realized_pnl=eq["realized"],
                                               unrealized_pnl=eq["unrealized"], max_drawdown=st.max_drawdown_pct)

    # ------------------------------------------------------------------ reports
    def daily_report_data(self, day: str | None = None) -> dict[str, Any]:
        now = self.clock.now()
        day = day or utc_day(now)
        start = datetime.fromisoformat(day + "T00:00:00+00:00")
        stats = self.repo.trade_stats(self.mode, since=start)
        by_strategy = {}
        recs = []
        for sid in self.bots:
            s = self.repo.trade_stats(self.mode, sid, since=start)
            by_strategy[sid] = {"trades": int(s["n"]), "pnl": float(s["pnl"])}
            if sid != "arbitrage":
                recs.extend(f"{sid}: {r.message}" for r in self.performance.recommendations(sid, self.mode))
        eq = self.equity_breakdown()
        dd = self.risk.drawdown.peek()
        return {
            "date": day, "mode": self.mode.value, "equity": eq["equity"],
            "day_start": float(dd.get("day_start", eq["equity"])),
            "daily_pnl": eq["equity"] - float(dd.get("day_start", eq["equity"])),
            "max_drawdown": float(dd.get("max_dd", 0.0)), "trades_today": int(stats["n"]),
            "wins": int(stats["wins"]), "losses": int(stats["losses"]), "fees": float(stats["fees"]),
            "by_strategy": by_strategy, "recommendations": recs,
        }

    async def maybe_daily_report(self) -> None:
        now = self.clock.now()
        today = utc_day(now)
        if self._last_report_day is None:
            self._last_report_day = today
            self.repo.set_kv(f"pm:{self.mode.value}:last_report_day", today)
            return
        if self._last_report_day != today:
            data = self.daily_report_data(self._last_report_day)
            self.repo.upsert_daily_metrics(self._last_report_day, self.mode, "ALL",
                                           trades_count=data["trades_today"], wins=data["wins"],
                                           losses=data["losses"], fees_usd=data["fees"])
            await self.notifier.notify("daily_report", tpl.daily_report(data))
            self._last_report_day = today
            self.repo.set_kv(f"pm:{self.mode.value}:last_report_day", today)

    # ------------------------------------------------------------------ control API (Telegram, scripts)
    def status_data(self) -> dict[str, Any]:
        st = self.risk.last_status
        return {
            "mode": self.mode.value, "locks": self.risk.blocking_locks(),
            "bots": [b.describe() for b in self.bots.values()],
            "equity": self.last_equity, "daily_pnl": st.daily_pnl if st else None,
            "drawdown": st.drawdown_pct if st else None,
        }

    def balance_data(self) -> dict[str, Any]:
        eq = self.equity_breakdown()
        active = self.repo.list_active_orders(self.mode)
        caps = self.allocator.summary(self.positions.all(), active, self.positions.realized_by_strategy())
        return {
            "mode": self.mode.value, "equity": eq["equity"], "cash": eq["cash"], "invested": eq["invested"],
            "reserve": self.config.capital.reserve_usd,
            "strategies": {sid: {"allocation": c.allocation, "deployed": c.deployed, "reserved": c.reserved,
                                 "available": c.available} for sid, c in caps.items()},
        }

    def pnl_data(self) -> dict[str, Any]:
        eq = self.equity_breakdown()
        s = self.repo.trade_stats(self.mode)
        st = self.risk.last_status
        return {"mode": self.mode.value, "realized": eq["realized"], "unrealized": eq["unrealized"],
                "daily_pnl": st.daily_pnl if st else None,
                "total_pnl": eq["equity"] - self.config.capital.total_capital_usd,
                "fees": sum(p.fees_paid for p in self.positions.all()), "trades": int(s["n"])}

    def positions_data(self) -> list[dict[str, Any]]:
        return [{"strategy_id": p.strategy_id, "symbol": p.symbol, "quantity": p.quantity, "avg": p.avg_entry_price,
                 "last": p.last_price, "upnl": p.unrealized_pnl, "stop": p.stop_price}
                for p in self.positions.all(open_only=True)]

    def risk_data(self) -> dict[str, Any]:
        d = self.risk.status_summary()
        exp = self.engine.exposure.snapshot(self.positions.all(open_only=True), self.repo.list_active_orders(self.mode))
        d["exposure"] = exp.total
        return d

    def pause(self, reason: str = "operator") -> None:
        self.risk.pause(reason)

    def resume(self) -> tuple[bool, list[str]]:
        return self.risk.resume()

    async def stop_trading(self) -> int:
        """/stop: stop bots and cancel unfilled entry (buy) orders. Positions are kept."""
        for bot in self.bots.values():
            bot.paused = True
        self.risk.pause("stop command")
        return await self.engine.cancel_where(side=Side.BUY, reason="stop command")

    async def emergency_stop(self, reason: str = "operator") -> int:
        """Immediately block ALL new orders, then cancel open orders. Never liquidates positions."""
        self.risk.emergency_stop(reason)  # first: blocks order placement synchronously
        for bot in self.bots.values():
            bot.paused = True
        n = await self.engine.cancel_where(reason="emergency stop")
        await self.notifier.notify("emergency_stop", f"🚨 EMERGENCY STOP ({reason}). New orders blocked, "
                                   f"{n} open orders canceled. Positions kept.", "CRITICAL")
        return n

    def paper_validation(self) -> dict[str, Any]:
        """Progress towards the MINIMUM paper criteria (not a guarantee of future returns)."""
        first = self.repo.first_equity_ts(Mode.PAPER)
        days = (self.clock.now() - first).days if first else 0
        s = self.repo.trade_stats(Mode.PAPER)
        critical = self.repo.query(
            "SELECT COUNT(*) FROM risk_events WHERE mode='paper' AND event_type IN "
            "('OVERFILL','LIMIT_VIOLATION','UNEXPECTED_FILL','RECONCILIATION_FAILED')")[0][0]
        pc = self.config.paper
        return {
            "days": days, "min_days": pc.min_days, "closed_trades": int(s["n"]), "min_trades": pc.min_closed_trades,
            "net_pnl": float(s["pnl"]), "critical_execution_errors": int(critical),
            "days_ok": days >= pc.min_days, "trades_ok": int(s["n"]) >= pc.min_closed_trades,
            "no_critical_errors": int(critical) == 0, "net_positive": float(s["pnl"]) > 0,
        }
