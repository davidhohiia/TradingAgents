"""Backtest engine: no look-ahead, realistic fills, metrics, chronological splits, walk-forward."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from trinity.backtesting.engine import BacktestConfig, BacktestEngine, BarContext, BtOrder
from trinity.backtesting.metrics import compute_metrics, max_drawdown
from trinity.backtesting.strategies import GridBacktest, TrendBacktest
from trinity.backtesting.walk_forward import chronological_split, optimize_holdout, walk_forward


def bars(closes, volume=1000.0, wick=0.01):
    closes = np.asarray(closes, float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="15min", tz="UTC")
    return pd.DataFrame({"open": opens, "high": np.maximum(opens, closes) * (1 + wick),
                         "low": np.minimum(opens, closes) * (1 - wick), "close": closes,
                         "volume": volume}, index=idx)


class Recorder:
    def __init__(self, orders_at=None):
        self.seen = []
        self.orders_at = orders_at or {}

    def on_bar(self, ctx: BarContext):
        self.seen.append((ctx.index, len(ctx.history), ctx.history.index[-1]))
        return list(self.orders_at.get(ctx.index, [])), []


def cfg(**kw):
    base = dict(initial_cash=1000, maker_fee=0.001, taker_fee=0.002, spread_bps=0, slippage_bps=0,
                participation_rate=1.0, qty_step=1e-6, min_notional=0, periods_per_year=252, lookback_bars=10_000)
    base.update(kw)
    return BacktestConfig(**base)


def test_no_lookahead_history_ends_at_current_bar():
    df = bars(np.linspace(100, 110, 50))
    rec = Recorder()
    BacktestEngine(cfg()).run(df, rec)
    for i, n, last_ts in rec.seen:
        assert n == i + 1 and last_ts == df.index[i]


def test_market_order_fills_next_bar_open_with_fee():
    df = bars([100, 101, 105, 106, 107])
    rec = Recorder({1: [BtOrder("buy", "market", 1.0)]})
    res = BacktestEngine(cfg()).run(df, rec)
    f = res.fills[0]
    assert f.index == 2 and f.price == pytest.approx(df["open"].iloc[2])  # not the decision bar's close
    assert f.fee == pytest.approx(f.price * 0.002)


def test_limit_requires_trade_through_and_partial_fills():
    df = bars([100] * 10, volume=10, wick=0.0)
    df.loc[df.index[3], "low"] = 99.0  # touches exactly 99 -> NOT a fill for a 99 limit
    df.loc[df.index[5], "low"] = 98.5  # trades through
    rec = Recorder({1: [BtOrder("buy", "limit", 5.0, 99.0)]})
    res = BacktestEngine(cfg(participation_rate=0.2)).run(df, rec)
    assert [f.index for f in res.fills] == [5]
    assert res.fills[0].qty == pytest.approx(2.0)  # 20% of 10 volume -> partial
    assert res.fills[0].fee == pytest.approx(2.0 * 99.0 * 0.001)  # maker fee


def test_spread_and_slippage_reduce_results():
    df = bars(np.linspace(100, 101, 30))
    def orders():
        return {1: [BtOrder("buy", "market", 1.0)], 10: [BtOrder("sell", "market", 1.0)]}

    clean = BacktestEngine(cfg()).run(df, Recorder(orders())).metrics.net_profit
    costly = BacktestEngine(cfg(spread_bps=20, slippage_bps=20)).run(df, Recorder(orders())).metrics.net_profit
    assert costly < clean


def test_stop_order_gap_fills_at_open():
    df = bars([100, 100, 100, 90, 90], wick=0.0)
    def orders():
        return {0: [BtOrder("buy", "market", 1.0)], 1: [BtOrder("sell", "stop", 1.0, 95.0)]}

    res = BacktestEngine(cfg()).run(df, Recorder(orders()))
    stop_fill = [f for f in res.fills if f.side == "sell"][0]
    assert stop_fill.price == pytest.approx(100.0) or stop_fill.price <= 95.0
    df2 = bars([100, 100, 100, 100, 100], wick=0.0)
    df2.loc[df2.index[3], ["open", "low", "close"]] = [90.0, 89.0, 90.0]
    res2 = BacktestEngine(cfg()).run(df2, Recorder(orders()))
    assert [f for f in res2.fills if f.side == "sell"][0].price == pytest.approx(90.0)  # gap risk modelled


def test_metrics():
    assert max_drawdown([100, 120, 90, 130]) == pytest.approx(0.25)
    m = compute_metrics([100, 101, 99, 103], [2.0, -1.0, 3.0], 0.5, 252, benchmark_return=0.01)
    assert m.trades == 3 and m.win_rate == pytest.approx(2 / 3) and m.profit_factor == pytest.approx(5.0)
    assert m.avg_trade == pytest.approx(4 / 3) and m.total_fees == 0.5 and m.sharpe is not None
    assert compute_metrics([100, 100], [], 0, 252).win_rate is None


def test_chronological_split():
    s = chronological_split(1000, 0.6, 0.2, warmup=100)
    assert s.train == (100, 640) and s.validation == (640, 820) and s.test == (820, 1000)
    with pytest.raises(ValueError):
        chronological_split(100, 0.9, 0.2)


def _grid_df(n=900, seed=5):
    rng = np.random.default_rng(seed)
    closes = 60000 * np.exp(np.cumsum(rng.normal(0, 0.003, n)))
    return bars(closes, volume=5.0, wick=0.003)


def test_grid_backtest_runs_with_same_strategy_code(config):
    df = _grid_df()
    m = config.instruments["BTC/USDT"].to_market_info("BTC/USDT")
    bt = cfg(initial_cash=150, maker_fee=0.001, taker_fee=0.001, qty_step=m.qty_step, min_notional=5,
             participation_rate=0.05, lookback_bars=300)
    res = BacktestEngine(bt).run(df, GridBacktest(config.strategies.crypto_grid, m, 150, 0.01), start_index=80)
    assert res.metrics.trades >= 0 and math.isfinite(res.metrics.net_return)
    buys = [f for f in res.fills if f.side == "buy"]
    assert all(f.qty * f.price <= 20.0 + 1e-6 for f in buys)  # fixed level size
    assert res.metrics.total_fees >= 0


def test_holdout_and_walk_forward_never_test_on_training_data(config):
    df = _grid_df(700)
    m = config.instruments["BTC/USDT"].to_market_info("BTC/USDT")
    bt = cfg(initial_cash=150, maker_fee=0.001, taker_fee=0.001, qty_step=m.qty_step, min_notional=5,
             participation_rate=0.05, lookback_bars=200)

    def factory(params):
        c = config.strategies.crypto_grid.model_copy(deep=True)
        c.grid.atr_spacing_multiplier = params["k"]
        return GridBacktest(c, m, 150, 0.01)

    split = chronological_split(len(df), warmup=80)
    res = optimize_holdout(df, factory, {"k": [0.5, 1.0]}, bt, split, min_trades=1)
    assert res.best_params["k"] in (0.5, 1.0)
    folds = walk_forward(df, factory, {"k": [0.5, 1.0]}, bt, warmup=80, train_bars=250, test_bars=120, min_trades=1)
    assert folds
    for f in folds:
        assert f.test[0] >= f.train[1]  # out-of-sample strictly after training
    tests = [f.test for f in folds]
    assert all(a[1] <= b[0] for a, b in zip(tests, tests[1:], strict=False))  # non-overlapping


def test_trend_backtest_runs(config):
    rng = np.random.default_rng(2)
    closes = 400 * np.cumprod(1 + 0.001 + rng.normal(0, 0.008, 400))
    idx = pd.date_range("2024-01-01", periods=400, freq="D", tz="UTC")
    df = bars(closes, volume=5e6).set_axis(idx)
    m = config.instruments["SPY"].to_market_info("SPY")
    res = BacktestEngine(cfg(initial_cash=150, maker_fee=0, taker_fee=0, qty_step=m.qty_step, min_notional=1,
                             participation_rate=0.01)).run(
        df, TrendBacktest(config.strategies.stock_trend, m, 150, 0.01, 75), start_index=60)
    assert res.metrics.benchmark_return is not None and res.metrics.max_drawdown < 0.5
