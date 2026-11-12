"""Position sizing based on stop-loss distance (fixed fractional risk, no martingale)."""

from __future__ import annotations

from dataclasses import dataclass

from trinity.core.indicators import floor_to_step


@dataclass(frozen=True)
class SizingResult:
    quantity: float
    notional: float
    risk_usd: float
    limited_by: str
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.quantity > 0


def risk_of(quantity: float, entry: float, stop: float) -> float:
    return abs(entry - stop) * abs(quantity)


def size_by_stop(
    *,
    strategy_capital: float,
    risk_pct: float,
    entry: float,
    stop: float,
    max_order_notional: float,
    max_position_notional: float,
    available_cash: float,
    qty_step: float,
    min_qty: float,
    min_notional: float,
    fee_rate: float = 0.0,
) -> SizingResult:
    """quantity = (capital * risk_pct) / |entry - stop|, then capped and rounded DOWN.

    The size never depends on previous wins/losses (no martingale). Because the
    strategy capital shrinks after losses (profits are not reinvested), the size can
    only stay the same or get smaller after losses.
    """
    if entry <= 0 or stop <= 0:
        return SizingResult(0, 0, 0, "invalid", "entry/stop must be positive")
    if stop >= entry:
        return SizingResult(0, 0, 0, "invalid", "stop must be below entry for long positions")
    if strategy_capital <= 0 or risk_pct <= 0:
        return SizingResult(0, 0, 0, "capital", "no capital allocated")
    risk_budget = strategy_capital * risk_pct
    per_unit_risk = entry - stop
    candidates = {
        "risk": risk_budget / per_unit_risk,
        "max_order_notional": max_order_notional / entry,
        "max_position_notional": max(max_position_notional, 0) / entry,
        "available_cash": max(available_cash, 0) / (entry * (1 + fee_rate)),
    }
    limited_by = min(candidates, key=lambda k: candidates[k])
    qty = floor_to_step(candidates[limited_by], qty_step)
    notional = qty * entry
    if qty <= 0 or qty < min_qty - 1e-12:
        return SizingResult(0, 0, 0, limited_by, f"quantity below minimum (limited by {limited_by})")
    if notional < min_notional - 1e-9:
        return SizingResult(0, 0, 0, limited_by, f"notional {notional:.2f} below minimum {min_notional}")
    return SizingResult(qty, notional, risk_of(qty, entry, stop), limited_by)


def size_fixed_notional(
    *, notional: float, price: float, qty_step: float, min_qty: float, min_notional: float
) -> SizingResult:
    """Fixed-notional sizing (used by grid levels). Never scaled up after losses."""
    if price <= 0 or notional <= 0:
        return SizingResult(0, 0, 0, "invalid", "price/notional must be positive")
    qty = floor_to_step(notional / price, qty_step)
    if qty < min_qty - 1e-12 or qty <= 0:
        return SizingResult(0, 0, 0, "min_qty", "quantity below minimum")
    value = qty * price
    if value < min_notional - 1e-9:
        return SizingResult(0, 0, 0, "min_notional", f"notional {value:.2f} below minimum {min_notional}")
    return SizingResult(qty, value, 0.0, "fixed")
