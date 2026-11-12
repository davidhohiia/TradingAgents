"""Backtest performance metrics."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np


@dataclass
class Metrics:
    net_return: float
    net_profit: float
    win_rate: float | None
    profit_factor: float | None
    max_drawdown: float
    sharpe: float | None
    sortino: float | None
    avg_trade: float | None
    trades: int
    total_fees: float
    benchmark_return: float | None = None
    exposure_time: float | None = None

    def as_dict(self) -> dict[str, float | int | None]:
        return asdict(self)


def max_drawdown(equity: list[float] | np.ndarray) -> float:
    eq = np.asarray(equity, dtype=float)
    if eq.size == 0:
        return 0.0
    peak = np.maximum.accumulate(eq)
    dd = (peak - eq) / np.where(peak > 0, peak, 1)
    return float(dd.max())


def sharpe(returns: np.ndarray, periods_per_year: float) -> float | None:
    if returns.size < 2 or returns.std(ddof=1) == 0:
        return None
    return float(returns.mean() / returns.std(ddof=1) * math.sqrt(periods_per_year))


def sortino(returns: np.ndarray, periods_per_year: float) -> float | None:
    if returns.size < 2:
        return None
    downside = returns[returns < 0]
    if downside.size < 2:
        return None
    dd = math.sqrt(float((downside**2).mean()))
    if dd == 0:
        return None
    return float(returns.mean() / dd * math.sqrt(periods_per_year))


def compute_metrics(equity: list[float], trade_pnls: list[float], fees: float, periods_per_year: float,
                    benchmark_return: float | None = None, exposure_time: float | None = None) -> Metrics:
    eq = np.asarray(equity, dtype=float)
    rets = np.diff(eq) / eq[:-1] if eq.size > 1 else np.array([])
    wins = [p for p in trade_pnls if p > 0]
    losses = [-p for p in trade_pnls if p < 0]
    n = len(trade_pnls)
    pf = (sum(wins) / sum(losses)) if losses else (math.inf if wins else None)
    return Metrics(
        net_return=float(eq[-1] / eq[0] - 1) if eq.size else 0.0,
        net_profit=float(eq[-1] - eq[0]) if eq.size else 0.0,
        win_rate=len(wins) / n if n else None,
        profit_factor=pf,
        max_drawdown=max_drawdown(eq),
        sharpe=sharpe(rets, periods_per_year),
        sortino=sortino(rets, periods_per_year),
        avg_trade=sum(trade_pnls) / n if n else None,
        trades=n,
        total_fees=fees,
        benchmark_return=benchmark_return,
        exposure_time=exposure_time,
    )
