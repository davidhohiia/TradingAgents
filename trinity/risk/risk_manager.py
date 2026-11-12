"""Global Risk Manager.

Every order intent from every bot is checked here immediately before submission.
No bot and no AI module can bypass these checks: the execution engine is the only
component holding trading-capable exchange adapters and it always calls
``RiskManager.check_order`` first.

Lock hierarchy (persisted in the database, survive restarts):
    emergency_stop   -> blocks ALL orders. Cleared only locally (scripts/emergency_stop.py --clear).
    recovery_lock    -> set after DB restore / failed reconciliation. Blocks non-protective orders.
    drawdown_lock    -> max drawdown reached. Live trading disabled. Only protective reduce-only exits.
                        Cleared only by manual local confirmation (scripts/emergency_stop.py --confirm-drawdown).
    daily_lock_day   -> daily loss limit hit. Only reduce-only orders until next UTC day.
    paused           -> /pause. Only reduce-only orders. /resume clears ONLY this flag.
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
from trinity.core.models import (
    MarketInfo,
    Mode,
    OrderIntent,
    OrderType,
    RiskDecision,
    Side,
)
from trinity.database.repository import Repository
from trinity.risk.circuit_breaker import CircuitBreakerRegistry
from trinity.risk.drawdown_monitor import DrawdownMonitor, DrawdownStatus
from trinity.risk.exposure_manager import ExposureSnapshot

log = get_logger(__name__)

MAX_LIMIT_PRICE_DEVIATION = 0.03  # fat-finger guard: limit price within 3% of reference
DRAWDOWN_CONFIRM_PHRASE = "CONFIRM DRAWDOWN REVIEW"


@dataclass
class RiskState:
    paused: bool = False
    emergency_stop: bool = False
    drawdown_lock: bool = False
    recovery_lock: bool = False
    daily_lock_day: str | None = None
    live_disabled: bool = False
    drawdown_warning_day: str | None = None
    reasons: dict[str, str] = field(default_factory=dict)

    def daily_locked(self, day: str) -> bool:
        return self.daily_lock_day == day


@dataclass
class RiskContext:
    """Everything the risk manager needs to evaluate a single order."""

    exposure: ExposureSnapshot
    market: MarketInfo
    strategy_capital: float  # allocation adjusted for realized losses
    strategy_available: float  # free budget according to the capital allocator
    now: datetime
    venue_connected: bool = True


@dataclass
class RiskAction:
    kind: str  # DAILY_LOCK | DRAWDOWN_LOCK | DRAWDOWN_WARNING
    message: str
    details: dict[str, Any] = field(default_factory=dict)


class RiskManager:
    def __init__(
        self,
        config: AppConfig,
        repo: Repository,
        mode: Mode,
        breakers: CircuitBreakerRegistry,
        clock: Clock | None = None,
    ) -> None:
        self.config = config
        self.rc = config.risk
        self.repo = repo
        self.mode = mode
        self.breakers = breakers
        self.clock = clock or RealClock()
        self.initial_capital = config.capital.total_capital_usd
        self.drawdown = DrawdownMonitor(repo, mode, self.initial_capital)
        self._state_key = f"risk:{mode.value}:state"
        self._recent_orders: deque[datetime] = deque()
        self.last_status: DrawdownStatus | None = None

    # ------------------------------------------------------------------ state persistence
    @property
    def state(self) -> RiskState:
        raw = self.repo.get_kv(self._state_key)
        if not raw:
            return RiskState()
        return RiskState(**raw)

    def _save(self, state: RiskState) -> None:
        self.repo.set_kv(self._state_key, asdict(state))

    def _event(self, severity: str, event_type: str, message: str, **details: Any) -> None:
        self.repo.add_risk_event(
            self.mode, severity, event_type, message,
            strategy_id=details.pop("strategy_id", None), symbol=details.pop("symbol", None),
            details=details, ts=self.clock.now(),
        )

    # ------------------------------------------------------------------ operator controls
    def pause(self, reason: str = "operator") -> None:
        s = self.state
        s.paused = True
        s.reasons["paused"] = reason
        self._save(s)
        self._event("WARNING", "PAUSED", f"trading paused: {reason}")

    def resume(self) -> tuple[bool, list[str]]:
        """Clears ONLY the pause flag. Never clears drawdown/emergency/recovery locks."""
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
        self._event("WARNING", "DRAWDOWN_LOCK_CLEARED",
                    "drawdown lock cleared by manual confirmation; peak reset", equity=current_equity)
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
        day = utc_day(self.clock.now())
        locks = []
        if s.emergency_stop:
            locks.append("emergency_stop")
        if s.recovery_lock:
            locks.append("recovery_lock")
        if s.drawdown_lock:
            locks.append("drawdown_lock")
        if s.daily_locked(day):
            locks.append("daily_loss_lock")
        if s.paused:
            locks.append("paused")
        if self.mode == Mode.LIVE and s.live_disabled:
            locks.append("live_disabled")
        return locks

    def is_locked(self) -> bool:
        return bool(self.blocking_locks())

    # ------------------------------------------------------------------ equity monitoring
    def on_equity(self, equity: float) -> list[RiskAction]:
        now = self.clock.now()
        status = self.drawdown.update(equity, now)
        self.last_status = status
        actions: list[RiskAction] = []
        s = self.state
        changed = False
        max_daily_loss = self.rc.max_daily_loss_pct * self.initial_capital
        if status.daily_pnl <= -max_daily_loss and not s.daily_locked(status.day):
            s.daily_lock_day = status.day
            s.reasons["daily_lock"] = f"daily PnL {status.daily_pnl:.2f} <= -{max_daily_loss:.2f}"
            changed = True
            msg = f"Daily loss limit reached: {status.daily_pnl:.2f} USD (limit -{max_daily_loss:.2f})"
            self._event("CRITICAL", "DAILY_LOSS_LIMIT", msg, equity=equity)
            actions.append(RiskAction("DAILY_LOCK", msg, {"daily_pnl": status.daily_pnl}))
        if status.drawdown_pct >= self.rc.max_drawdown_pct and not s.drawdown_lock:
            s.drawdown_lock = True
            s.live_disabled = True
            s.reasons["drawdown_lock"] = f"drawdown {status.drawdown_pct:.2%}"
            changed = True
            msg = (f"Max drawdown reached: {status.drawdown_pct:.2%} (limit {self.rc.max_drawdown_pct:.2%}). "
                   "Live trading disabled. Manual confirmation required.")
            self._event("CRITICAL", "MAX_DRAWDOWN", msg, equity=equity, peak=status.peak_equity)
            actions.append(RiskAction("DRAWDOWN_LOCK", msg, {"drawdown": status.drawdown_pct}))
        elif (status.drawdown_pct >= self.rc.drawdown_warning_pct
              and s.drawdown_warning_day != status.day and not s.drawdown_lock):
            s.drawdown_warning_day = status.day
            changed = True
            msg = f"Drawdown warning: {status.drawdown_pct:.2%} (lock at {self.rc.max_drawdown_pct:.2%})"
            self._event("WARNING", "DRAWDOWN_WARNING", msg, equity=equity)
            actions.append(RiskAction("DRAWDOWN_WARNING", msg, {"drawdown": status.drawdown_pct}))
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

        s = self.state
        day = utc_day(ctx.now)
        key = (intent.strategy_id, intent.symbol)
        held = ctx.exposure.position_qty.get(key, 0.0)
        pending_sells = ctx.exposure.pending_sell_qty.get(key, 0.0)
        is_reducing = intent.side == Side.SELL
        protective = is_reducing and intent.protective_exit

        # 1. Global locks
        check("emergency_stop", not s.emergency_stop, "emergency stop active: all orders blocked")
        if self.mode == Mode.LIVE:
            check("live_enabled", not s.live_disabled, "live trading disabled by risk system")
        if not protective:
            check("recovery_lock", not s.recovery_lock, "recovery/reconciliation lock active")
        check("drawdown_lock", not s.drawdown_lock or protective,
              "max drawdown lock: only protective exits allowed")
        check("daily_lock", not s.daily_locked(day) or is_reducing,
              "daily loss limit reached: new positions blocked until next UTC day")
        check("paused", not s.paused or is_reducing, "trading paused: only reducing orders allowed")

        # 2. Basic validity
        qty_ok = math.isfinite(intent.quantity) and intent.quantity > 0
        price_ok = math.isfinite(intent.reference_price) and intent.reference_price > 0
        check("quantity_valid", qty_ok, f"invalid quantity {intent.quantity}")
        check("price_valid", price_ok, f"invalid reference price {intent.reference_price}")
        if intent.order_type == OrderType.LIMIT:
            check("limit_price_set", intent.price is not None and intent.price > 0, "limit order without price")
        if not (qty_ok and price_ok):
            return RiskDecision(False, reasons, checks)
        check("market_active", ctx.market.active, f"{intent.symbol} not tradable on venue")

        # 3. Market data freshness and venue health
        age = (ctx.now - intent.market_data_ts).total_seconds()
        limit = self.rc.price_age_limit(intent.asset_class)
        check("fresh_data", -5 <= age <= limit, f"stale market data: {age:.0f}s old (limit {limit:.0f}s)")
        check("venue_breaker", self.breakers.allow(intent.venue),
              f"circuit breaker open for {intent.venue}: API failures")
        check("venue_connected", ctx.venue_connected, f"{intent.venue} not connected")

        # 4. Order rate limit
        cutoff = ctx.now - timedelta(seconds=60)
        while self._recent_orders and self._recent_orders[0] < cutoff:
            self._recent_orders.popleft()
        check("order_rate", len(self._recent_orders) < self.rc.max_orders_per_minute,
              "order rate limit exceeded (possible runaway loop)")
        check("max_open_orders", ctx.exposure.open_orders < self.rc.max_open_orders,
              f"too many open orders ({ctx.exposure.open_orders})")

        # 5. Fat-finger guard for limit prices
        if intent.order_type == OrderType.LIMIT and intent.price:
            dev = (intent.price - intent.reference_price) / intent.reference_price
            if intent.side == Side.BUY:
                check("limit_price_sane", dev <= MAX_LIMIT_PRICE_DEVIATION,
                      f"buy limit {dev:.2%} above market")
            else:
                check("limit_price_sane", dev >= -MAX_LIMIT_PRICE_DEVIATION,
                      f"sell limit {dev:.2%} below market")

        if is_reducing:
            # 6a. No short selling: sells must be covered by the position (incl. other pending sells)
            check("no_short", intent.quantity <= held - pending_sells + 1e-9,
                  f"sell {intent.quantity} exceeds free position {held - pending_sells:.8f} (short selling forbidden)")
        else:
            # 6b. Risk-increasing order checks
            notional = intent.notional
            fee = ctx.market.taker_fee if intent.order_type == OrderType.MARKET else ctx.market.maker_fee
            check("min_notional", notional >= max(self.rc.min_order_notional_usd, ctx.market.min_notional) - 1e-9,
                  f"order notional {notional:.2f} below minimum")
            check("min_qty", intent.quantity >= ctx.market.min_qty - 1e-12, "quantity below venue minimum")
            check("max_order_notional", notional <= self.rc.max_order_notional_usd + 1e-9,
                  f"order notional {notional:.2f} > max {self.rc.max_order_notional_usd:.2f}")
            check("strategy_capital", ctx.strategy_capital > 0, f"{intent.strategy_id} has no allocated capital")
            check("strategy_budget", notional * (1 + fee) <= ctx.strategy_available + 1e-9,
                  f"insufficient strategy budget: need {notional * (1 + fee):.2f}, available {ctx.strategy_available:.2f}")
            if intent.stop_price is None or intent.stop_price <= 0 or intent.stop_price >= intent.effective_price:
                check("stop_defined", False, "risk-increasing order requires a protective stop below entry")
            else:
                risk_usd = intent.quantity * (intent.effective_price - intent.stop_price)
                max_risk = self.rc.max_risk_per_trade_pct * ctx.strategy_capital
                check("risk_per_trade", risk_usd <= max_risk + 1e-9,
                      f"trade risk {risk_usd:.2f} > max {max_risk:.2f} ({self.rc.max_risk_per_trade_pct:.2%})")
            max_total = self.rc.max_total_exposure_pct * self.initial_capital
            check("total_exposure", ctx.exposure.total + notional <= max_total + 1e-9,
                  f"total exposure {ctx.exposure.total + notional:.2f} > max {max_total:.2f}")
            group = self.config.correlation_group(intent.symbol)
            max_group = self.rc.max_correlated_exposure_pct * self.initial_capital
            group_exp = ctx.exposure.by_group.get(group, 0.0) + notional
            check("correlated_exposure", group_exp <= max_group + 1e-9,
                  f"correlated exposure in {group} {group_exp:.2f} > max {max_group:.2f}")
            max_pos = self.rc.max_position_pct_of_strategy * ctx.strategy_capital
            pos_exp = ctx.exposure.by_strategy_symbol.get(key, 0.0) + notional
            check("position_size", pos_exp <= max_pos + 1e-9,
                  f"position {intent.symbol} {pos_exp:.2f} > max {max_pos:.2f}")
            new_position = key not in ctx.exposure.open_positions
            n_pos = len(ctx.exposure.open_positions) + (1 if new_position else 0)
            check("max_open_positions", n_pos <= self.rc.max_open_positions,
                  f"open positions {n_pos} > max {self.rc.max_open_positions}")

        approved = not reasons
        if not approved:
            log.info("risk rejected %s %s %s: %s", intent.strategy_id, intent.side, intent.symbol, "; ".join(reasons))
        return RiskDecision(approved, reasons, checks)

    def status_summary(self) -> dict[str, Any]:
        s = self.state
        st = self.last_status
        return {
            "mode": self.mode.value,
            "locks": self.blocking_locks(),
            "reasons": s.reasons,
            "equity": st.equity if st else None,
            "peak_equity": st.peak_equity if st else None,
            "drawdown_pct": st.drawdown_pct if st else None,
            "daily_pnl": st.daily_pnl if st else None,
            "max_daily_loss_usd": self.rc.max_daily_loss_pct * self.initial_capital,
            "max_drawdown_pct": self.rc.max_drawdown_pct,
            "max_total_exposure_usd": self.rc.max_total_exposure_pct * self.initial_capital,
            "breakers": self.breakers.snapshot(),
        }
