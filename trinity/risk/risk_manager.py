"""Global Risk Manager (TRINITY 3.0).

Every order intent from every bot is checked here immediately before submission. No bot
and no AI module can change or bypass these limits: the execution engine is the only
component holding trading-capable venue adapters and it always calls ``check_order`` first.

Percentages are of CURRENT ACCOUNT EQUITY (contributed capital + net realized/unrealized PnL):
  risk per trade 0.75% | total open risk 3% | daily loss 3% | weekly loss 6%
Drawdown tiers (from the deposit-adjusted equity peak):
  10% warning -> 12% reduced activity (risk x0.5) -> 15% LIVE LOCK (new positions blocked,
  open orders canceled, manual confirmation) -> 20% HARD STOP (only protective exits).

Locks (persisted in the DB, survive restarts):
  emergency_stop  manual; blocks every NEW position; only reducing exits allowed so open
                  positions stay under control (SPEC_4.0 A3); cleared only locally
  hard_stop       20% drawdown; protective exits only; cleared only locally
  drawdown_lock   15% drawdown; protective exits only; manual confirmation
  recovery_lock   DB restored / reconciliation failed; protective exits only
  weekly_lock     weekly loss limit; reducing orders only until next ISO week
  daily_lock      daily loss limit; reducing orders only until next UTC day
  paused          /pause; reducing orders only; /resume clears ONLY this flag
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from trinity.config.settings import AppConfig
from trinity.core.clock import Clock, RealClock, utc_day
from trinity.core.log import get_logger
from trinity.core.models import AssetClass, MarketInfo, Mode, OrderIntent, OrderType, RiskDecision, Side
from trinity.database.repository import Repository
from trinity.risk.circuit_breaker import CircuitBreakerRegistry
from trinity.risk.correlation import correlated_with
from trinity.risk.drawdown_monitor import DrawdownMonitor, DrawdownStatus, iso_week
from trinity.risk.exposure_manager import ExposureSnapshot
from trinity.risk.leverage import LeverageGuard
from trinity.risk.profiles import get_profile, profile_violations

log = get_logger(__name__)

MAX_LIMIT_PRICE_DEVIATION = 0.03
DRAWDOWN_CONFIRM_PHRASE = "CONFIRM DRAWDOWN REVIEW"
HARD_STOP_CLEAR_PHRASE = "CLEAR HARD STOP"


@dataclass
class RiskState:
    paused: bool = False
    emergency_stop: bool = False
    hard_stop: bool = False
    drawdown_lock: bool = False
    recovery_lock: bool = False
    daily_lock_day: str | None = None
    weekly_lock_week: str | None = None
    live_disabled: bool = False
    warning_days: dict[str, str] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)

    def daily_locked(self, day: str) -> bool:
        return self.daily_lock_day == day

    def weekly_locked(self, week: str) -> bool:
        return self.weekly_lock_week == week


@dataclass
class RiskContext:
    exposure: ExposureSnapshot
    market: MarketInfo
    strategy_capital: float
    strategy_available: float
    now: datetime
    venue_connected: bool = True
    equity: float = 0.0
    open_risk: float = 0.0  # risk-to-stop of open positions + pending entries
    held_symbols: list[str] = field(default_factory=list)
    funding_rate_8h: float | None = None


@dataclass
class RiskAction:
    kind: str  # DAILY_LOCK | WEEKLY_LOCK | DRAWDOWN_WARNING | REDUCED_ACTIVITY | DRAWDOWN_LOCK | HARD_STOP
    message: str
    details: dict[str, Any] = field(default_factory=dict)


class RiskManager:
    def __init__(self, config: AppConfig, repo: Repository, mode: Mode, breakers: CircuitBreakerRegistry,
                 clock: Clock | None = None, leverage_guard: LeverageGuard | None = None) -> None:
        self.config = config
        self.rc = config.risk
        self.repo = repo
        self.mode = mode
        self.breakers = breakers
        self.clock = clock or RealClock()
        self.initial_capital = config.capital.total_capital_usd
        self.drawdown = DrawdownMonitor(repo, mode, self.initial_capital)
        self.leverage_guard = leverage_guard or LeverageGuard(config.leverage, None, repo, mode)
        self._state_key = f"risk:{mode.value}:state"
        self._recent_orders: deque[datetime] = deque()
        self.last_status: DrawdownStatus | None = None
        self.correlations: dict[tuple[str, str], float] = {}
        self.tests_ok: bool | None = None
        self.profile = get_profile(config.profile)
        self._integrity_alerted = False
        bad = profile_violations(self.profile, config.risk, config.quality)
        if bad:
            raise ValueError("risk configuration does not match profile: " + "; ".join(bad))

    # ------------------------------------------------------------------ state
    @property
    def state(self) -> RiskState:
        raw = self.repo.get_kv(self._state_key)
        if not raw:
            return RiskState()
        known = RiskState.__dataclass_fields__
        return RiskState(**{k: v for k, v in raw.items() if k in known})

    def _save(self, state: RiskState) -> None:
        self.repo.set_kv(self._state_key, asdict(state))

    def _event(self, severity: str, event_type: str, message: str, **details: Any) -> None:
        self.repo.add_risk_event(self.mode, severity, event_type, message,
                                 strategy_id=details.pop("strategy_id", None), symbol=details.pop("symbol", None),
                                 details=details, ts=self.clock.now())

    def update_correlations(self, matrix: dict[tuple[str, str], float]) -> None:
        self.correlations = dict(matrix)

    # ------------------------------------------------------------------ operator controls
    def pause(self, reason: str = "operator") -> None:
        s = self.state
        s.paused = True
        s.reasons["paused"] = reason
        self._save(s)
        self._event("WARNING", "PAUSED", f"trading paused: {reason}")

    def resume(self) -> tuple[bool, list[str]]:
        """Clears ONLY the pause flag. Never clears drawdown/hard-stop/emergency/recovery locks."""
        s = self.state
        s.paused = False
        s.reasons.pop("paused", None)
        self._save(s)
        remaining = self.blocking_locks()
        self._event("INFO", "RESUMED", "pause cleared", remaining_locks=remaining)
        return (not remaining, remaining)

    def emergency_stop(self, reason: str) -> None:
        s = self.state
        s.emergency_stop = True
        s.live_disabled = True
        s.reasons["emergency_stop"] = reason
        self._save(s)
        self._event("CRITICAL", "EMERGENCY_STOP", f"emergency stop: {reason}")

    def clear_emergency_stop(self, operator_confirmation: str) -> bool:
        if operator_confirmation != "CLEAR EMERGENCY STOP":
            return False
        s = self.state
        s.emergency_stop = False
        s.reasons.pop("emergency_stop", None)
        self._save(s)
        self._event("WARNING", "EMERGENCY_CLEARED", "emergency stop cleared manually (live stays disabled)")
        return True

    def confirm_drawdown_review(self, operator_confirmation: str, current_equity: float) -> bool:
        if operator_confirmation != DRAWDOWN_CONFIRM_PHRASE:
            return False
        s = self.state
        s.drawdown_lock = False
        s.reasons.pop("drawdown_lock", None)
        self._save(s)
        self.drawdown.reset_peak(current_equity)
        self._event("WARNING", "DRAWDOWN_LOCK_CLEARED", "drawdown lock cleared by manual confirmation; peak reset",
                    equity=current_equity)
        return True

    def clear_hard_stop(self, operator_confirmation: str, current_equity: float) -> bool:
        if operator_confirmation != HARD_STOP_CLEAR_PHRASE:
            return False
        s = self.state
        s.hard_stop = False
        s.drawdown_lock = False
        s.reasons.pop("hard_stop", None)
        s.reasons.pop("drawdown_lock", None)
        self._save(s)
        self.drawdown.reset_peak(current_equity)
        self._event("CRITICAL", "HARD_STOP_CLEARED", "20% hard stop cleared manually; peak reset",
                    equity=current_equity)
        return True

    def set_recovery_lock(self, reason: str) -> None:
        s = self.state
        s.recovery_lock = True
        s.reasons["recovery_lock"] = reason
        self._save(s)
        self._event("CRITICAL", "RECOVERY_LOCK", reason)

    def clear_recovery_lock(self) -> None:
        s = self.state
        if s.recovery_lock:
            s.recovery_lock = False
            s.reasons.pop("recovery_lock", None)
            self._save(s)
            self._event("INFO", "RECOVERY_LOCK_CLEARED", "reconciliation succeeded")

    def disable_live(self, reason: str) -> None:
        s = self.state
        s.live_disabled = True
        s.reasons["live_disabled"] = reason
        self._save(s)
        self._event("CRITICAL", "LIVE_DISABLED", reason)

    def blocking_locks(self) -> list[str]:
        s = self.state
        now = self.clock.now()
        locks = []
        for flag in ("emergency_stop", "hard_stop", "recovery_lock", "drawdown_lock"):
            if getattr(s, flag):
                locks.append(flag)
        if s.weekly_locked(iso_week(now)):
            locks.append("weekly_loss_lock")
        if s.daily_locked(utc_day(now)):
            locks.append("daily_loss_lock")
        if s.paused:
            locks.append("paused")
        if self.mode == Mode.LIVE and s.live_disabled:
            locks.append("live_disabled")
        return locks

    def is_locked(self) -> bool:
        return bool(self.blocking_locks())

    def activity_multiplier(self) -> float:
        """Rule-based reduced activity at the 12% drawdown tier (never set by AI)."""
        st = self.last_status
        if st is not None and st.drawdown_pct >= self.rc.drawdown_reduce_pct:
            return self.rc.reduced_activity_multiplier
        return 1.0

    # ------------------------------------------------------------------ equity monitoring
    def on_equity(self, equity: float) -> list[RiskAction]:
        now = self.clock.now()
        st = self.drawdown.update(equity, now)
        self.last_status = st
        actions: list[RiskAction] = []
        s = self.state
        changed = False
        rc = self.rc
        if st.daily_pnl <= -rc.max_daily_loss_pct * st.day_start_equity and not s.daily_locked(st.day):
            s.daily_lock_day = st.day
            s.reasons["daily_lock"] = f"daily PnL {st.daily_pnl:.2f} ({st.daily_pnl_pct:.2%})"
            changed = True
            msg = f"Daily loss limit reached: {st.daily_pnl:.2f} USD ({st.daily_pnl_pct:.2%}, limit -{rc.max_daily_loss_pct:.0%})"
            self._event("CRITICAL", "DAILY_LOSS_LIMIT", msg, equity=equity)
            actions.append(RiskAction("DAILY_LOCK", msg, {"daily_pnl": st.daily_pnl}))
        if st.weekly_pnl <= -rc.max_weekly_loss_pct * st.week_start_equity and not s.weekly_locked(st.week):
            s.weekly_lock_week = st.week
            s.reasons["weekly_lock"] = f"weekly PnL {st.weekly_pnl:.2f} ({st.weekly_pnl_pct:.2%})"
            changed = True
            msg = f"Weekly loss limit reached: {st.weekly_pnl:.2f} USD ({st.weekly_pnl_pct:.2%}, limit -{rc.max_weekly_loss_pct:.0%})"
            self._event("CRITICAL", "WEEKLY_LOSS_LIMIT", msg, equity=equity)
            actions.append(RiskAction("WEEKLY_LOCK", msg, {"weekly_pnl": st.weekly_pnl}))
        dd = st.drawdown_pct
        if dd >= rc.drawdown_emergency_pct and not s.hard_stop:
            s.hard_stop = True
            s.drawdown_lock = True
            s.live_disabled = True
            s.reasons["hard_stop"] = f"drawdown {dd:.2%}"
            changed = True
            msg = (f"ABSOLUTE DRAWDOWN LIMIT {dd:.2%} >= {rc.drawdown_emergency_pct:.0%}: hard stop. "
                   "Only protective exits allowed. Manual local confirmation required.")
            self._event("CRITICAL", "HARD_STOP", msg, equity=equity, peak=st.peak_equity)
            actions.append(RiskAction("HARD_STOP", msg, {"drawdown": dd}))
        elif dd >= rc.drawdown_live_lock_pct and not s.drawdown_lock:
            s.drawdown_lock = True
            s.live_disabled = True
            s.reasons["drawdown_lock"] = f"drawdown {dd:.2%}"
            changed = True
            msg = (f"Drawdown {dd:.2%} >= {rc.drawdown_live_lock_pct:.0%}: new positions blocked, live trading "
                   "disabled, open orders canceled. Manual confirmation required.")
            self._event("CRITICAL", "DRAWDOWN_LIVE_LOCK", msg, equity=equity, peak=st.peak_equity)
            actions.append(RiskAction("DRAWDOWN_LOCK", msg, {"drawdown": dd}))
        elif dd >= rc.drawdown_reduce_pct and s.warning_days.get("reduce") != st.day:
            s.warning_days["reduce"] = st.day
            changed = True
            msg = (f"Drawdown {dd:.2%} >= {rc.drawdown_reduce_pct:.0%}: reduced activity "
                   f"(risk x{rc.reduced_activity_multiplier})")
            self._event("WARNING", "REDUCED_ACTIVITY", msg, equity=equity)
            actions.append(RiskAction("REDUCED_ACTIVITY", msg, {"drawdown": dd}))
        elif dd >= rc.drawdown_warning_pct and s.warning_days.get("warning") != st.day:
            s.warning_days["warning"] = st.day
            changed = True
            msg = f"Drawdown warning: {dd:.2%} (reduced activity at {rc.drawdown_reduce_pct:.0%}, lock at {rc.drawdown_live_lock_pct:.0%})"
            self._event("WARNING", "DRAWDOWN_WARNING", msg, equity=equity)
            actions.append(RiskAction("DRAWDOWN_WARNING", msg, {"drawdown": dd}))
        if changed:
            self._save(s)
        return actions

    # ------------------------------------------------------------------ order check
    def record_submission(self, now: datetime) -> None:
        self._recent_orders.append(now)

    def check_order(self, intent: OrderIntent, ctx: RiskContext) -> RiskDecision:
        checks: dict[str, bool] = {}
        reasons: list[str] = []

        def check(name: str, ok: bool, reason: str) -> None:
            checks[name] = ok
            if not ok:
                reasons.append(reason)

        rc = self.rc
        s = self.state
        day, week = utc_day(ctx.now), iso_week(ctx.now)
        key = (intent.strategy_id, intent.symbol)
        held = ctx.exposure.position_qty.get(key, 0.0)
        pending_sells = ctx.exposure.pending_sell_qty.get(key, 0.0)
        is_reducing = intent.side == Side.SELL
        protective = is_reducing and intent.protective_exit

        # 1. locks
        # 0. BALANCED profile integrity (limits may never be changed at runtime, by AI or anything else)
        violations = profile_violations(self.profile, self.config.risk, self.config.quality)
        if violations or self.config.risk is not self.rc:
            msg = "risk profile integrity violation: " + ("; ".join(violations) or "risk config object replaced")
            if not self._integrity_alerted:
                self._integrity_alerted = True
                self._event("CRITICAL", "PROFILE_INTEGRITY", msg)
            check("profile_integrity", is_reducing and intent.protective_exit, msg)
        check("emergency_stop", not s.emergency_stop or is_reducing,
              "emergency stop active: new positions blocked (only reducing exits allowed)")
        if self.mode == Mode.LIVE:
            check("live_enabled", not s.live_disabled or protective, "live trading disabled by risk system")
        check("hard_stop", not s.hard_stop or protective, "20% hard stop: only protective exits allowed")
        check("recovery_lock", not s.recovery_lock or protective, "recovery/reconciliation lock active")
        check("drawdown_lock", not s.drawdown_lock or protective, "15% drawdown lock: only protective exits allowed")
        check("weekly_lock", not s.weekly_locked(week) or is_reducing, "weekly loss limit: new positions blocked")
        check("daily_lock", not s.daily_locked(day) or is_reducing, "daily loss limit: new positions blocked")
        check("paused", not s.paused or is_reducing, "trading paused: only reducing orders allowed")

        # 2. validity
        qty_ok = math.isfinite(intent.quantity) and intent.quantity > 0
        price_ok = math.isfinite(intent.reference_price) and intent.reference_price > 0
        check("quantity_valid", qty_ok, f"invalid quantity {intent.quantity}")
        check("price_valid", price_ok, f"invalid reference price {intent.reference_price}")
        if intent.order_type == OrderType.LIMIT:
            check("limit_price_set", intent.price is not None and intent.price > 0, "limit order without price")
        if not (qty_ok and price_ok):
            return RiskDecision(False, reasons, checks)
        check("market_active", ctx.market.active, f"{intent.symbol} not tradable on venue")

        # 3. leverage (spot by default; anything else only through the LeverageGuard)
        if intent.leverage > 1.0 or intent.leverage < 1.0:
            if intent.leverage < 1.0:
                check("leverage_valid", False, "invalid leverage")
            elif intent.asset_class == AssetClass.STOCK:
                check("no_stock_leverage", False, "leverage/margin is forbidden for stocks")
            else:
                lev_reasons = self.leverage_guard.check_intent(intent, ctx.now, ctx.funding_rate_8h, self.tests_ok)
                check("leverage_guard", not lev_reasons, "; ".join(lev_reasons))

        # 4. data / venue health
        age = (ctx.now - intent.market_data_ts).total_seconds()
        limit = rc.price_age_limit(intent.asset_class)
        check("fresh_data", -5 <= age <= limit, f"stale market data: {age:.0f}s old (limit {limit:.0f}s)")
        check("venue_breaker", self.breakers.allow(intent.venue), f"circuit breaker open for {intent.venue}: API failures")
        check("venue_connected", ctx.venue_connected, f"{intent.venue} not connected")

        # 5. runaway protection
        cutoff = ctx.now - timedelta(seconds=60)
        while self._recent_orders and self._recent_orders[0] < cutoff:
            self._recent_orders.popleft()
        check("order_rate", len(self._recent_orders) < rc.max_orders_per_minute,
              "order rate limit exceeded (possible runaway loop)")
        check("max_open_orders", ctx.exposure.open_orders < rc.max_open_orders,
              f"too many open orders ({ctx.exposure.open_orders})")
        if intent.order_type == OrderType.LIMIT and intent.price:
            dev = (intent.price - intent.reference_price) / intent.reference_price
            if intent.side == Side.BUY:
                check("limit_price_sane", dev <= MAX_LIMIT_PRICE_DEVIATION, f"buy limit {dev:.2%} above market")
            else:
                check("limit_price_sane", dev >= -MAX_LIMIT_PRICE_DEVIATION, f"sell limit {dev:.2%} below market")

        if is_reducing:
            check("no_short", intent.quantity <= held - pending_sells + 1e-9,
                  f"sell {intent.quantity} exceeds free position {held - pending_sells:.8f} (short selling forbidden)")
            return self._finish(intent, reasons, checks)

        # 6. risk-increasing order
        threshold = self.config.quality.threshold(intent.strategy_id)
        if intent.quality_score is None:
            check("quality_score", False, "entry without Trade Quality Score is not allowed")
        else:
            check("quality_score", intent.quality_score >= threshold,
                  f"Trade Quality Score {intent.quality_score:.0f} < threshold {threshold} ({self.profile.name})")
        equity = ctx.equity if ctx.equity > 0 else self.initial_capital
        mult = self.activity_multiplier()
        notional = intent.notional
        margin = notional / max(intent.leverage, 1.0)
        fee = ctx.market.taker_fee if intent.order_type == OrderType.MARKET else ctx.market.maker_fee
        check("min_notional", notional >= max(rc.min_order_notional_usd, ctx.market.min_notional) - 1e-9,
              f"order notional {notional:.2f} below minimum")
        check("min_qty", intent.quantity >= ctx.market.min_qty - 1e-12, "quantity below venue minimum")
        check("max_order_notional", notional <= rc.max_order_notional_usd + 1e-9,
              f"order notional {notional:.2f} > max {rc.max_order_notional_usd:.2f}")
        check("strategy_capital", ctx.strategy_capital > 0, f"{intent.strategy_id} has no allocated capital")
        check("strategy_budget", margin + notional * fee <= ctx.strategy_available + 1e-9,
              f"insufficient strategy budget: need {margin + notional * fee:.2f}, available {ctx.strategy_available:.2f}")
        trade_risk = 0.0
        if intent.stop_price is None or intent.stop_price <= 0 or intent.stop_price >= intent.effective_price:
            check("stop_defined", False, "risk-increasing order requires a protective stop below entry")
        else:
            trade_risk = intent.quantity * (intent.effective_price - intent.stop_price)
            max_risk = rc.max_risk_per_trade_pct * equity * mult
            check("risk_per_trade", trade_risk <= max_risk + 1e-9,
                  f"trade risk {trade_risk:.2f} > max {max_risk:.2f} ({rc.max_risk_per_trade_pct:.2%} x{mult} of equity)")
            max_open = rc.max_total_open_risk_pct * equity * mult
            check("total_open_risk", ctx.open_risk + trade_risk <= max_open + 1e-9,
                  f"total open risk {ctx.open_risk + trade_risk:.2f} > max {max_open:.2f} "
                  f"({rc.max_total_open_risk_pct:.0%} x{mult} of equity)")
        max_total = rc.max_total_exposure_pct * equity
        check("total_exposure", ctx.exposure.total + notional <= max_total + 1e-9,
              f"total exposure {ctx.exposure.total + notional:.2f} > max {max_total:.2f}")
        group = self.config.correlation_group(intent.symbol)
        max_group = rc.max_correlated_exposure_pct * equity
        group_exp = ctx.exposure.by_group.get(group, 0.0) + notional
        check("correlated_exposure", group_exp <= max_group + 1e-9,
              f"correlated exposure in {group} {group_exp:.2f} > max {max_group:.2f}")
        others = [sym for (sid, sym) in ctx.exposure.open_positions if sym == intent.symbol and sid != intent.strategy_id]
        check("same_instrument", not others, f"{intent.symbol} already held by another strategy")
        clash = correlated_with(intent.symbol, ctx.held_symbols, self.correlations, rc.correlation_threshold)
        check("dynamic_correlation", not clash,
              f"{intent.symbol} highly correlated with open position {clash[0][0]} ({clash[0][1]:.2f})" if clash else "")
        max_pos = rc.max_position_pct_of_strategy * ctx.strategy_capital * max(intent.leverage, 1.0)
        pos_exp = ctx.exposure.by_strategy_symbol.get(key, 0.0) + notional
        check("position_size", pos_exp <= max_pos + 1e-9, f"position {intent.symbol} {pos_exp:.2f} > max {max_pos:.2f}")
        new_position = key not in ctx.exposure.open_positions
        n_pos = len(ctx.exposure.open_positions) + (1 if new_position else 0)
        check("max_open_positions", n_pos <= rc.max_open_positions,
              f"open positions {n_pos} > max {rc.max_open_positions}")
        return self._finish(intent, reasons, checks)

    def _finish(self, intent: OrderIntent, reasons: list[str], checks: dict[str, bool]) -> RiskDecision:
        approved = not reasons
        if not approved:
            log.info("risk rejected %s %s %s: %s", intent.strategy_id, intent.side, intent.symbol, "; ".join(reasons))
        return RiskDecision(approved, reasons, checks)

    def max_trade_risk_usd(self, equity: float) -> float:
        return self.rc.max_risk_per_trade_pct * equity * self.activity_multiplier()

    def status_summary(self) -> dict[str, Any]:
        s = self.state
        st = self.last_status
        equity = st.equity if st else self.initial_capital
        return {
            "mode": self.mode.value, "locks": self.blocking_locks(), "reasons": s.reasons,
            "equity": st.equity if st else None, "peak_equity": st.peak_equity if st else None,
            "drawdown_pct": st.drawdown_pct if st else None,
            "daily_pnl": st.daily_pnl if st else None, "weekly_pnl": st.weekly_pnl if st else None,
            "max_daily_loss_usd": self.rc.max_daily_loss_pct * (st.day_start_equity if st else equity),
            "max_weekly_loss_usd": self.rc.max_weekly_loss_pct * (st.week_start_equity if st else equity),
            "drawdown_tiers": {"warning": self.rc.drawdown_warning_pct, "reduce": self.rc.drawdown_reduce_pct,
                               "live_lock": self.rc.drawdown_live_lock_pct, "hard_stop": self.rc.drawdown_emergency_pct},
            "max_drawdown_pct": self.rc.drawdown_live_lock_pct,
            "activity_multiplier": self.activity_multiplier(),
            "max_risk_per_trade_usd": self.max_trade_risk_usd(equity),
            "max_total_open_risk_usd": self.rc.max_total_open_risk_pct * equity * self.activity_multiplier(),
            "max_total_exposure_usd": self.rc.max_total_exposure_pct * equity,
            "breakers": self.breakers.snapshot(),
            "leverage_enabled": self.config.leverage.enabled,
        }
