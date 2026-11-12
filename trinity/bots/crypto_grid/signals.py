"""Grid signal math (pure functions, shared by live bot and backtests)."""

from __future__ import annotations

import math
from dataclasses import dataclass

from trinity.config.settings import GridParams
from trinity.core.indicators import round_to_step


@dataclass(frozen=True)
class NetProfit:
    gross: float
    fees: float
    spread_cost: float
    net: float
    net_pct: float


def adaptive_spacing(atr_pct: float, p: GridParams) -> float:
    """Distance between grid levels as a fraction of price, adapted to volatility (ATR)."""
    if not math.isfinite(atr_pct) or atr_pct <= 0:
        return p.min_spacing_pct
    return min(max(atr_pct * p.atr_spacing_multiplier, p.min_spacing_pct), p.max_spacing_pct)


def buy_levels(mid: float, spacing: float, levels: int, price_step: float) -> list[float]:
    """Levels strictly below mid: mid*(1-s)^i, i=1..levels (geometric grid)."""
    out = []
    for i in range(1, levels + 1):
        px = round_to_step(mid * (1 - spacing) ** i, price_step)
        if px > 0 and (not out or px < out[-1]):
            out.append(px)
    return out


def grid_stop(lowest_level: float, atr_value: float, k: float, price_step: float) -> float:
    return max(round_to_step(lowest_level - k * atr_value, price_step), price_step)


def expected_net_profit(
    buy_price: float, sell_price: float, qty: float, maker_fee: float, spread_pct: float, spread_buffer: float
) -> NetProfit:
    """Expected net result of one buy->sell grid cycle after fees and spread cost."""
    gross = qty * (sell_price - buy_price)
    fees = qty * buy_price * maker_fee + qty * sell_price * maker_fee
    spread_cost = qty * buy_price * max(spread_pct, 0.0) * spread_buffer
    net = gross - fees - spread_cost
    notional = qty * buy_price
    return NetProfit(gross, fees, spread_cost, net, net / notional if notional > 0 else 0.0)


def sell_target(buy_price: float, spacing: float, price_step: float) -> float:
    raw = buy_price * (1 + spacing)
    n = math.ceil(raw / price_step - 1e-9)
    return round(n * price_step, 10)
