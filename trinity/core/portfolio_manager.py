"""Central Portfolio Manager.

Responsibilities: bot lifecycle (start/stop/pause, supervised restarts), capital
allocation overview, exposure and position overview, equity / PnL tracking, risk
coordination (daily-loss and drawdown reactions), daily reports, operator control
API used by Telegram and scripts.

Bots never talk to venues directly for trading: they hold an OrderGateway that routes
everything through the ExecutionEngine and the Global Risk Manager.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from trinity.ai.performance_analyzer import StrategyAnalytics
from trinity.core.capital_growth import CapitalGrowthManager
from trinity.core.position_guardian import PositionGuardian
from trinity.monitoring.api_monitor import ApiMonitor
from trinity.risk.leverage import funding_cost
from trinity.risk.risk_manager import RiskAction
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


@dataclass
class ResumeReport:
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append((name, ok, detail))

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c[1] for c in self.checks)

    def text(self) -> str:
        return "\n".join(f"{'✅' if ok else '❌'} {n}: {d}" for n, ok, d in self.checks)
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
        growth: CapitalGrowthManager | None = None,
        api_monitor: ApiMonitor | None = None,
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
        self.performance = StrategyAnalytics(repo)
        self.growth = growth or CapitalGrowthManager(repo, config, clock)
        # single source of truth for contributed capital and allocations
        engine.contributed_fn = self.growth.contributed
        engine.allocator._allocation_fn = self.growth.allocation  # noqa: SLF001
        engine.allocator._all_fn = self.growth.allocations  # noqa: SLF001
        self.api_monitor = api_monitor
        self.sheets: Any = None  # optional Google Sheets journal syncer (set by the app builder)
        self.guardian = PositionGuardian(engine, config, self.notifier, self.clock)
        self._paused_at: datetime | None = None
        self.sheets_interval = 60.0
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
        self.scheduler.add("position_guardian", self.config.emergency.guardian_interval_seconds,
                           self.guardian.run_once, timeout=60)
        self.scheduler.add("risk_monitor", 30, self.monitor, timeout=60)
        self.scheduler.add("daily_report", 300, self.maybe_daily_report, timeout=60)
        self.scheduler.add("analytics", 3600, self.analytics, timeout=120, run_immediately=False)
        self.scheduler.add("api_health", 300, self.api_health, timeout=30, run_immediately=False)
        self.scheduler.add("funding", 3600, self.accrue_funding, timeout=60, run_immediately=False)
        if self.sheets is not None:
            self.scheduler.add("sheets_sync", self.sheets_interval, self.sync_sheets, timeout=180,
                               run_immediately=False)
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
        """Equity = contributed capital (all deposits) + net realized + unrealized PnL.
        Deposits are never counted as profit."""
        t = self.positions.totals()
        contributed = self.growth.contributed()
        equity = contributed + t["realized"] + t["unrealized"]
        return {
            "equity": equity, "contributed": contributed, "trading_profit": equity - contributed,
            "realized": t["realized"], "unrealized": t["unrealized"],
            "invested": t["invested"], "market_value": t["market_value"],
            "cash": equity - t["market_value"],
        }

    async def monitor(self) -> None:
        await self.mark_to_market()
        eq = self.equity_breakdown()
        self.last_equity = eq["equity"]
        actions = self.risk.on_equity(eq["equity"])
        for a in actions:
            await self._handle_risk_action(a)
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

    async def _handle_risk_action(self, a: RiskAction) -> None:
        if a.kind in ("DAILY_LOCK", "WEEKLY_LOCK"):
            n = await self.engine.cancel_where(side=Side.BUY, reason=a.kind.lower())
            period = "UTC day" if a.kind == "DAILY_LOCK" else "ISO week"
            await self.notifier.notify("risk_limit", f"⛔ {a.message}. Canceled {n} entry orders. Positions kept; "
                                       f"new entries blocked until the next {period}.", "CRITICAL")
        elif a.kind in ("DRAWDOWN_LOCK", "HARD_STOP"):
            n = await self.engine.cancel_where(reason=a.kind.lower())  # unfilled orders only: always safe
            pos = self.positions_data()
            lines = [f"• {p['strategy_id']} {p['symbol']} {p['quantity']:g} @ {p['avg']:.2f} uPnL ${p['upnl']:+.2f} "
                     f"stop {p['stop'] if p['stop'] else 'n/a'}" for p in pos] or ["• no open positions"]
            how = ("scripts/emergency_stop.py --clear-hard-stop" if a.kind == "HARD_STOP"
                   else "scripts/emergency_stop.py --confirm-drawdown")
            self.repo.set_kv(f"pm:{self.mode.value}:positions_at_lock", pos)
            await self.notifier.notify(
                "risk_limit", f"🛑 {a.message}\nCanceled {n} unfilled orders. Positions were NOT liquidated "
                f"(protective stops stay active):\n" + "\n".join(lines)
                + f"\nEquity ${self.last_equity:.2f}. Resume only after review: {how}", "CRITICAL")
        elif a.kind == "REDUCED_ACTIVITY":
            await self.notifier.notify("drawdown_warning", f"⚠️ {a.message}", "WARNING")
        elif a.kind == "DRAWDOWN_WARNING":
            await self.notifier.notify("drawdown_warning", f"⚠️ {a.message}", "WARNING")

    # ------------------------------------------------------------------ analytics / monitoring tasks
    async def analytics(self) -> None:
        """Strategy degradation, real-vs-virtual divergence, correlation (alerts only, never parameter changes)."""
        deg = self.performance.degradation_report(self.mode)
        previous = self.repo.get_kv(f"analytics:{self.mode.value}:degraded") or {}
        self.repo.set_kv(f"analytics:{self.mode.value}:degraded", deg)
        for key, notes in deg.items():
            if key not in previous:
                self.repo.add_risk_event(self.mode, "WARNING", "STRATEGY_DEGRADATION", "; ".join(notes),
                                         strategy_id=key, ts=self.clock.now())
                await self.notifier.notify("degradation", f"📉 Strategy degradation detected: {key}: "
                                           + "; ".join(notes) + " (no parameters changed automatically)", "WARNING")
        self.repo.set_kv(f"analytics:{self.mode.value}:strategy_correlation",
                         self.performance.strategy_correlations(self.mode))

    async def api_health(self) -> None:
        if self.api_monitor is None:
            return
        self.api_monitor.flush(self.repo, self.clock.now())
        for problem in self.api_monitor.problems():
            await self.notifier.notify("api_error", problem, "WARNING")

    async def accrue_funding(self) -> None:
        """Funding on leveraged (isolated margin) positions. No-op while leverage is disabled."""
        now = self.clock.now()
        last = self.repo.get_kv(f"funding:{self.mode.value}:last")
        hours = 1.0 if last is None else (now - datetime.fromisoformat(last)).total_seconds() / 3600
        self.repo.set_kv(f"funding:{self.mode.value}:last", now.isoformat())
        for p in self.positions.all(open_only=True):
            if p.leverage <= 1.0:
                continue
            venue = self.engine.venues.get(p.venue)
            fetch = getattr(venue, "fetch_funding_rate", None)
            if fetch is None:
                continue
            try:
                rate = await fetch(p.symbol)
            except ExchangeError as exc:
                log.warning("funding rate unavailable: %s", exc)
                continue
            amount = funding_cost(p.market_value, rate, hours)
            self.positions.apply_funding(p.strategy_id, p.venue, p.symbol, amount, now)
            self.repo.add_funding_payment(now, self.mode, p.strategy_id, p.symbol, rate, p.market_value, amount)
            if rate > self.config.leverage.max_funding_rate_8h:
                await self.notifier.notify("funding", f"Funding {rate:.4%}/8h on {p.symbol} above limit", "WARNING")
            if p.liquidation_price and p.last_price and p.last_price <= p.liquidation_price * 1.05:
                self.repo.add_risk_event(self.mode, "CRITICAL", "LIQUIDATION_RISK",
                                         f"{p.symbol} price {p.last_price} within 5% of liquidation {p.liquidation_price}",
                                         strategy_id=p.strategy_id, symbol=p.symbol, ts=now)
                await self.notifier.notify("risk_limit", f"Liquidation risk on {p.symbol}", "CRITICAL")

    # ------------------------------------------------------------------ Google Sheets journal
    async def sync_sheets(self) -> None:
        """Send pending journal rows; failures are logged and retried on the next run (never block trading)."""
        if self.sheets is None:
            return
        res = await self.sheets.sync_all()
        if not res.ok:
            st = self.repo.get_kv("sheets:alerted") or {}
            day = utc_day(self.clock.now())
            if st.get("day") != day:  # at most one alert per day
                self.repo.set_kv("sheets:alerted", {"day": day})
                await self.notifier.notify("sheets", f"Google Sheets sync failed ({res.errors[0][:200]}). "
                                           f"{res.pending_left} trades kept locally and will be retried.", "WARNING")

    def journal_portfolio_data(self) -> dict[str, Any]:
        eq = self.equity_breakdown()
        deposits = self.repo.list_deposits()
        initial = sum(r["amount_usd"] for r in deposits if r["kind"] == "initial")
        st = self.risk.last_status
        return {"mode": self.mode.value, "ts": self.clock.now().isoformat(), "contributed": eq["contributed"],
                "initial_deposit": initial, "additional_deposits": eq["contributed"] - initial,
                "equity": eq["equity"], "cash": eq["cash"], "in_positions": eq["market_value"],
                "drawdown_pct": st.drawdown_pct if st else None, "reserve": self.growth.reserve()}

    # ------------------------------------------------------------------ capital growth
    def register_deposit(self, amount: float, note: str = "", currency: str = "USD", fx_rate: float = 1.0) -> int:
        return self.growth.register_deposit(amount, note=note, currency=currency, fx_rate=fx_rate)

    def propose_reallocation(self) -> tuple[list[int], list[str]]:
        perf = {sid: self.performance.strategy_performance(sid, self.mode).__dict__ for sid in self.bots}
        perf = {sid: {"trades": p["trades"], "net_pnl": p["net_pnl"], "profit_factor": p["profit_factor"],
                      "win_rate": p["win_rate"]} for sid, p in perf.items()}
        st = self.risk.last_status
        degraded = {k.split(".")[0] for k in (self.repo.get_kv(f"analytics:{self.mode.value}:degraded") or {})}
        return self.growth.propose(perf, self.risk.blocking_locks(), st.drawdown_pct if st else 0.0, degraded)

    def approve_reallocation(self, proposal_id: int, confirmation: str) -> tuple[bool, list[str]]:
        rows = self.repo.query("SELECT * FROM reallocation_proposals WHERE id=?", (proposal_id,))
        st = self.risk.last_status
        res = self.growth.approve(proposal_id, confirmation, self.risk.blocking_locks(), st.drawdown_pct if st else 0.0)
        if res.ok and rows and self.mode == Mode.PAPER:
            # paper: simulate the transfer from the (off-venue) reserve to the strategy's venue account
            sid, amount = rows[0]["strategy_id"], float(rows[0]["amount_usd"])
            bot = self.bots.get(sid)
            venue = self.engine.venues.get(getattr(getattr(bot, "gateway", None), "venue", ""), None)
            if venue is not None and hasattr(venue, "balances"):
                quote = "USDT" if sid == "crypto_momentum" else "USD"
                venue._bal(quote).free += amount  # noqa: SLF001
                venue._changed()  # noqa: SLF001
        return res.ok, res.reasons

    def capital_data(self) -> dict[str, Any]:
        eq = self.equity_breakdown()
        fees = sum(p.fees_paid for p in self.positions.all())
        summary = self.growth.summary(eq["equity"], fees)
        points = [(datetime.fromisoformat(r["ts"]), float(r["equity"])) for r in self.repo.equity_history(self.mode)]
        summary["monthly"] = self.growth.monthly_returns(points)
        summary["pending_proposals"] = self.growth.pending()
        summary["mode"] = self.mode.value
        return summary

    def strategies_data(self) -> dict[str, Any]:
        return {"mode": self.mode.value, "stats": self.performance.per_sub_strategy(self.mode),
                "real_vs_virtual": {sid: self.performance.real_vs_virtual(sid) for sid in self.bots
                                    if sid != "arbitrage"},
                "degraded": self.repo.get_kv(f"analytics:{self.mode.value}:degraded") or {},
                "strategy_correlation": self.repo.get_kv(f"analytics:{self.mode.value}:strategy_correlation") or {}}

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
            "contributed": eq["contributed"], "trading_profit": eq["trading_profit"],
            "weekly_pnl": eq["equity"] - float(dd.get("week_start", eq["equity"])),
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
            "reserve": self.growth.reserve(), "contributed": eq["contributed"],
            "strategies": {sid: {"allocation": c.allocation, "deployed": c.deployed, "reserved": c.reserved,
                                 "available": c.available} for sid, c in caps.items()},
        }

    def pnl_data(self) -> dict[str, Any]:
        eq = self.equity_breakdown()
        s = self.repo.trade_stats(self.mode)
        st = self.risk.last_status
        return {"mode": self.mode.value, "realized": eq["realized"], "unrealized": eq["unrealized"],
                "daily_pnl": st.daily_pnl if st else None,
                "total_pnl": eq["trading_profit"], "contributed": eq["contributed"],
                "fees": sum(p.fees_paid for p in self.positions.all()), "trades": int(s["n"])}

    def positions_data(self) -> list[dict[str, Any]]:
        return [{"strategy_id": p.strategy_id, "symbol": p.symbol, "quantity": p.quantity, "avg": p.avg_entry_price,
                 "last": p.last_price, "upnl": p.unrealized_pnl, "stop": p.stop_price}
                for p in self.positions.all(open_only=True)]

    def risk_data(self) -> dict[str, Any]:
        d = self.risk.status_summary()
        exp = self.engine.exposure.snapshot(self.positions.all(open_only=True), self.repo.list_active_orders(self.mode))
        d["exposure"] = exp.total
        d["open_risk"] = self.engine.open_risk()
        return d

    def pause(self, reason: str = "operator") -> None:
        """/pause: instant. Blocks NEW positions; open positions stay managed by bots and the guardian."""
        self.risk.pause(reason)
        self._paused_at = self.clock.now()
        for bot in self.bots.values():
            bot.paused = True

    async def resume_checks(self) -> ResumeReport:
        """The five checks required before /resume (SPEC_4.0 A2). Read-only except reconciliation."""
        rep = ResumeReport()
        # 1. venue connectivity
        for name, venue in self.engine.venues.items():
            sym = next((p.symbol for p in self.positions.all(open_only=True) if p.venue == name), None) or next(
                (s for s in self.config.instruments if self._venue_supports(venue, s)), None)
            try:
                if sym:
                    await venue.fetch_ticker(sym)
                rep.add(f"connection:{name}", self.engine.breakers.allow(name), "reachable"
                        if self.engine.breakers.allow(name) else "circuit breaker open")
            except ExchangeError as exc:
                rep.add(f"connection:{name}", False, f"unreachable: {str(exc)[:120]}")
        # 2. open positions are protected and priced
        unprotected = [f"{p.strategy_id}:{p.symbol}" for p in self.positions.all(open_only=True) if not p.stop_price]
        rep.add("positions", not unprotected, "all open positions have a stop" if not unprotected
                else "no stop on: " + ", ".join(unprotected))
        # 3. risk engine: only the pause may be cleared by /resume
        locks = [lk for lk in self.risk.blocking_locks() if lk != "paused"]
        rep.add("risk_engine", not locks, "no locks besides pause" if not locks
                else "locks that /resume cannot clear: " + ", ".join(locks))
        # 4. no critical execution errors since the pause, no open circuit breakers
        since = (self._paused_at or self.clock.now()).isoformat()
        crit = self.repo.query("SELECT event_type, message FROM risk_events WHERE ts>=? AND event_type IN "
                               "('OVERFILL','LIMIT_VIOLATION','UNEXPECTED_FILL','RECONCILIATION_FAILED',"
                               "'LIQUIDATION_RISK','PROFILE_INTEGRITY','CONNECTION_LOST')", (since,))
        restored = self.repo.query("SELECT COUNT(*) FROM risk_events WHERE ts>=? AND event_type='CONNECTION_RESTORED'",
                                   (since,))[0][0]
        crit = [r for r in crit if not (r["event_type"] == "CONNECTION_LOST" and restored)]
        open_breakers = [n for n, b in self.engine.breakers.snapshot().items() if b["state"] != "closed"]
        rep.add("critical_errors", not crit and not open_breakers,
                "none" if not crit and not open_breakers else
                "; ".join([f"{r['event_type']}: {r['message'][:80]}" for r in crit] + [f"breaker {b}" for b in open_breakers]))
        # 5. actual balances/orders/positions synchronised with the venue
        problems = []
        for name in self.engine.venues:
            r = await self.engine.reconcile(name)
            if not r.ok:
                problems.append(f"{name}: {', '.join(r.issues)[:150]}")
        rep.add("balance_sync", not problems, "venues reconciled" if not problems else "; ".join(problems))
        return rep

    @staticmethod
    def _venue_supports(venue: Any, symbol: str) -> bool:
        try:
            venue.market_info(symbol)
            return True
        except ExchangeError:
            return False

    async def resume_confirmed(self) -> ResumeReport:
        """Second step of /resume: re-run every check, then clear ONLY the pause flag."""
        rep = await self.resume_checks()
        if rep.ok:
            ok, remaining = self.risk.resume()
            if ok:
                for bot in self.bots.values():
                    bot.paused = False
            else:
                rep.add("risk_engine_after", False, "still blocked: " + ", ".join(remaining))
        return rep

    def resume(self) -> tuple[bool, list[str]]:
        """Low-level pause removal (local tools/tests). Telegram must use resume_checks + resume_confirmed."""
        return self.risk.resume()

    async def stop_trading(self) -> int:
        """/stop (confirmed): no new entries, unfilled entry orders canceled, positions stay managed."""
        self.pause("stop command")
        return await self.engine.cancel_where(side=Side.BUY, reason="stop command")

    async def emergency_stop(self, reason: str = "operator") -> dict[str, Any]:
        """Emergency protocol HOLD_PROTECTED (SPEC_4.0 A3). Instant; never leaves positions unmanaged."""
        self.risk.emergency_stop(reason)  # 1. synchronous: every new position is rejected from now on
        for bot in self.bots.values():  # 2. bots: no entries, exit management continues
            bot.paused = True
        active_buys = [o for o in self.repo.list_active_orders(self.mode) if o.side == Side.BUY]
        canceled = await self.engine.cancel_where(side=Side.BUY, reason="emergency stop")  # 3. safe cancels
        not_canceled = len([o for o in self.repo.list_active_orders(self.mode) if o.side == Side.BUY])
        guard = await self.guardian.run_once()  # 4+5. stops for every position, protective exits if breached
        positions = self.positions_data()
        lines = [f"• {p['strategy_id']} {p['symbol']} {p['quantity']:g} @ {p['avg']:.2f}, stop "
                 f"{p['stop']:.2f}" if p["stop"] else f"• {p['strategy_id']} {p['symbol']} {p['quantity']:g}: "
                 "NO STOP (price unavailable, guardian retrying)" for p in positions] or ["• no open positions"]
        report = {"canceled_entries": canceled, "entries_left": not_canceled, "entries_found": len(active_buys),
                  "positions": positions, "stops_assigned": guard.stops_assigned,
                  "protective_exits": guard.exits_submitted, "unguarded": guard.unguarded}
        self.repo.add_risk_event(self.mode, "CRITICAL", "EMERGENCY_PROTOCOL", f"HOLD_PROTECTED ({reason})",
                                 details=report, ts=self.clock.now())
        await self.notifier.notify(
            "emergency_stop",
            f"🚨 EMERGENCY STOP ({reason}). New positions blocked. Canceled {canceled} unfilled entry orders"
            + (f", {not_canceled} not canceled yet (retrying)" if not_canceled else "")
            + ". Positions are NOT liquidated; protective stops are enforced by the Position Guardian:\n"
            + "\n".join(lines) + ("\nUnguarded: " + "; ".join(guard.unguarded) if guard.unguarded else "")
            + "\nClear locally: scripts/emergency_stop.py --clear", "CRITICAL")
        return report

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
