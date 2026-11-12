"""Event-driven single-instrument backtest engine (bar by bar, no look-ahead).

Timing rule: a strategy sees bars [0..t] (bar t is CLOSED) and its orders can only
fill from bar t+1 onwards. Fill model (deliberately not perfect):
* market: next bar open +/- (half spread + slippage); size capped by participation
  rate x bar volume -> partial fills, remainder canceled (IOC);
* limit: fills only if the bar trades THROUGH the price (low < buy limit, high > sell limit);
  fill at the limit (or at the open if the bar gaps through it); capped by participation
  -> partial fills, remainder keeps resting;
* stop (sell): triggers when low <= stop; fills at min(open, stop) minus slippage (gap risk);
* maker fee for resting limit fills, taker fee for market/stop fills.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

import pandas as pd

from trinity.backtesting.metrics import Metrics, compute_metrics
from trinity.core.indicators import floor_to_step


@dataclass
class BacktestConfig:
    initial_cash: float = 150.0
    maker_fee: float = 0.001
    taker_fee: float = 0.001
    spread_bps: float = 2.0
    slippage_bps: float = 5.0
    participation_rate: float = 0.05
    qty_step: float = 1e-5
    min_notional: float = 5.0
    periods_per_year: float = 365 * 96  # 15m bars
    lookback_bars: int = 400  # history window passed to the strategy (past bars only)


@dataclass
class BtOrder:
    side: str  # "buy" | "sell"
    kind: str  # "market" | "limit" | "stop"
    qty: float
    price: float | None = None
    tag: str = ""
    id: int = 0
    filled: float = 0.0
    created_index: int = 0
    active: bool = True

    @property
    def remaining(self) -> float:
        return max(self.qty - self.filled, 0.0)


@dataclass
class BtFill:
    order_id: int
    index: int
    ts: datetime
    side: str
    qty: float
    price: float
    fee: float
    tag: str
    realized: float = 0.0


@dataclass
class BarContext:
    index: int
    ts: datetime
    history: pd.DataFrame  # bars [0..index], all closed
    position: float
    avg_cost: float
    cash: float
    open_orders: list[BtOrder]
    fills_last_bar: list[BtFill]


class BacktestStrategy(Protocol):
    def on_bar(self, ctx: BarContext) -> tuple[list[BtOrder], list[int]]:
        """Return (new orders, ids of orders to cancel)."""


@dataclass
class BacktestResult:
    metrics: Metrics
    equity: list[float]
    timestamps: list[datetime]
    fills: list[BtFill] = field(default_factory=list)
    trade_pnls: list[float] = field(default_factory=list)


class BacktestEngine:
    def __init__(self, cfg: BacktestConfig) -> None:
        self.cfg = cfg
        self._ids = itertools.count(1)

    def run(self, df: pd.DataFrame, strategy: BacktestStrategy, start_index: int = 0,
            end_index: int | None = None) -> BacktestResult:
        """Trade on bars [start_index, end_index). Bars before start_index are warm-up history only."""
        c = self.cfg
        end_index = len(df) if end_index is None else min(end_index, len(df))
        if start_index >= end_index - 1:
            raise ValueError("backtest window too short")
        cash, pos, avg = c.initial_cash, 0.0, 0.0
        orders: list[BtOrder] = []
        fills: list[BtFill] = []
        trade_pnls: list[float] = []
        equity: list[float] = []
        stamps: list[datetime] = []
        fees_total = 0.0
        bars_in_market = 0
        last_fills: list[BtFill] = []
        opens, highs, lows = df["open"].to_numpy(), df["high"].to_numpy(), df["low"].to_numpy()
        closes, vols = df["close"].to_numpy(), df["volume"].to_numpy()
        index = df.index
        half_spread = c.spread_bps / 20_000
        slip = c.slippage_bps / 10_000

        for t in range(start_index, end_index):
            # ---- 1. execute orders placed at t-1 or earlier against bar t
            bar_fills: list[BtFill] = []
            if t > start_index:
                capacity = vols[t] * c.participation_rate
                for o in orders:
                    if not o.active or o.remaining <= 0:
                        continue
                    if o.created_index >= t:
                        continue
                    px: float | None = None
                    fee_rate = c.taker_fee
                    if o.kind == "market":
                        px = opens[t] * (1 + half_spread + slip) if o.side == "buy" else opens[t] * (1 - half_spread - slip)
                    elif o.kind == "limit" and o.price is not None:
                        fee_rate = c.maker_fee
                        if o.side == "buy" and lows[t] < o.price:
                            px = min(o.price, opens[t])
                        elif o.side == "sell" and highs[t] > o.price:
                            px = max(o.price, opens[t])
                    elif o.kind == "stop" and o.price is not None and o.side == "sell" and lows[t] <= o.price:
                        px = min(opens[t], o.price) * (1 - slip)
                    if px is None:
                        continue
                    qty = floor_to_step(min(o.remaining, capacity), c.qty_step)
                    if o.side == "buy":
                        qty = min(qty, floor_to_step(cash / (px * (1 + fee_rate)), c.qty_step))
                    else:
                        qty = min(qty, floor_to_step(pos, c.qty_step) if pos > 0 else 0.0)
                    if qty <= 0:
                        if o.kind in ("market", "stop"):
                            o.active = False
                        continue
                    capacity -= qty
                    fee = qty * px * fee_rate
                    fees_total += fee
                    realized = 0.0
                    if o.side == "buy":
                        cash -= qty * px + fee
                        avg = (avg * pos + qty * px + fee) / (pos + qty)
                        pos += qty
                    else:
                        realized = (px - avg) * qty - fee
                        cash += qty * px - fee
                        pos -= qty
                        if pos <= 1e-12:
                            pos, avg = 0.0, 0.0
                        trade_pnls.append(realized)
                    o.filled += qty
                    f = BtFill(o.id, t, index[t], o.side, qty, px, fee, o.tag, realized)
                    fills.append(f)
                    bar_fills.append(f)
                    if o.kind in ("market", "stop") or o.remaining <= 1e-12:
                        o.active = False
                orders = [o for o in orders if o.active]
            last_fills = bar_fills
            # ---- 2. mark to market at close
            eq = cash + pos * closes[t] * (1 - half_spread)
            equity.append(eq)
            stamps.append(index[t])
            if pos > 0:
                bars_in_market += 1
            # ---- 3. strategy decides using data up to and including bar t (closed)
            if t < end_index - 1:
                hist = df.iloc[max(0, t + 1 - c.lookback_bars): t + 1]
                ctx = BarContext(t, index[t], hist, pos, avg, cash, list(orders), last_fills)
                new, cancel = strategy.on_bar(ctx)
                cancel_set = set(cancel)
                for o in orders:
                    if o.id in cancel_set:
                        o.active = False
                orders = [o for o in orders if o.active]
                for o in new:
                    if o.qty <= 0:
                        continue
                    ref = o.price or closes[t]
                    if o.side == "buy" and o.qty * ref < c.min_notional:
                        continue
                    o.id = next(self._ids)
                    o.created_index = t
                    orders.append(o)
        bench = closes[end_index - 1] / opens[start_index + 1] - 1 if end_index - start_index > 1 else None
        n_bars = end_index - start_index
        m = compute_metrics(equity, trade_pnls, fees_total, c.periods_per_year, benchmark_return=float(bench),
                            exposure_time=bars_in_market / n_bars if n_bars else None)
        return BacktestResult(m, equity, stamps, fills, trade_pnls)


def run_backtest(df: pd.DataFrame, make_strategy: Callable[[], BacktestStrategy], cfg: BacktestConfig,
                 start_index: int = 0, end_index: int | None = None) -> BacktestResult:
    return BacktestEngine(cfg).run(df, make_strategy(), start_index, end_index)
