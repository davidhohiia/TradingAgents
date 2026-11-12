"""Chronological splits and walk-forward validation.

* train / validation / test are strictly chronological, never shuffled;
* parameters are optimized on TRAIN, selected on VALIDATION, and the final TEST
  window is evaluated exactly once with the selected parameters;
* walk-forward: rolling (train -> test) folds; only out-of-sample results are reported.
Warm-up bars before a window are used for indicators only (past data, no look-ahead).
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from trinity.backtesting.engine import BacktestConfig, BacktestEngine, BacktestStrategy
from trinity.backtesting.metrics import Metrics

StrategyFactory = Callable[[dict[str, Any]], BacktestStrategy]


@dataclass(frozen=True)
class Split:
    train: tuple[int, int]
    validation: tuple[int, int]
    test: tuple[int, int]


def chronological_split(n: int, train: float = 0.6, validation: float = 0.2, warmup: int = 0) -> Split:
    if not 0 < train < 1 or not 0 < validation < 1 or train + validation >= 1:
        raise ValueError("invalid split fractions")
    usable = n - warmup
    a = warmup + int(usable * train)
    b = a + int(usable * validation)
    return Split((warmup, a), (a, b), (b, n))


def score(m: Metrics, min_trades: int = 5) -> float:
    """Objective: net return penalized by drawdown; too few trades -> rejected."""
    if m.trades < min_trades:
        return -math.inf
    return m.net_return - 0.5 * m.max_drawdown


def param_grid(space: dict[str, list[Any]]) -> list[dict[str, Any]]:
    keys = list(space)
    return [dict(zip(keys, combo, strict=True)) for combo in itertools.product(*(space[k] for k in keys))]


@dataclass
class HoldoutResult:
    best_params: dict[str, Any]
    train: Metrics
    validation: Metrics
    test: Metrics
    candidates: list[tuple[dict[str, Any], float]] = field(default_factory=list)


def optimize_holdout(df: pd.DataFrame, factory: StrategyFactory, space: dict[str, list[Any]], cfg: BacktestConfig,
                     split: Split, top_k: int = 3, min_trades: int = 5) -> HoldoutResult:
    engine = BacktestEngine(cfg)
    scored = []
    for params in param_grid(space):
        m = engine.run(df, factory(params), *split.train).metrics
        scored.append((params, score(m, min_trades), m))
    scored.sort(key=lambda x: x[1], reverse=True)
    finalists = [s for s in scored if math.isfinite(s[1])][:top_k] or scored[:1]
    best, best_val = None, -math.inf
    best_train = best_valm = None
    for params, _, tm in finalists:
        vm = engine.run(df, factory(params), *split.validation).metrics
        v = score(vm, max(1, min_trades // 2))
        if best is None or v > best_val:
            best, best_val, best_train, best_valm = params, v, tm, vm
    assert best is not None and best_train is not None and best_valm is not None
    test_m = engine.run(df, factory(best), *split.test).metrics  # evaluated ONCE
    return HoldoutResult(best, best_train, best_valm, test_m, [(p, s) for p, s, _ in scored])


@dataclass
class WalkForwardFold:
    train: tuple[int, int]
    test: tuple[int, int]
    params: dict[str, Any]
    test_metrics: Metrics


def walk_forward(df: pd.DataFrame, factory: StrategyFactory, space: dict[str, list[Any]], cfg: BacktestConfig,
                 *, warmup: int, train_bars: int, test_bars: int, min_trades: int = 5) -> list[WalkForwardFold]:
    engine = BacktestEngine(cfg)
    folds: list[WalkForwardFold] = []
    start = warmup
    while start + train_bars + test_bars <= len(df):
        tr = (start, start + train_bars)
        te = (tr[1], tr[1] + test_bars)
        best, best_s = None, -math.inf
        for params in param_grid(space):
            s = score(engine.run(df, factory(params), *tr).metrics, min_trades)
            if best is None or s > best_s:
                best, best_s = params, s
        assert best is not None
        folds.append(WalkForwardFold(tr, te, best, engine.run(df, factory(best), *te).metrics))
        start += test_bars
    return folds


def summarize_folds(folds: list[WalkForwardFold]) -> dict[str, float]:
    if not folds:
        return {"folds": 0}
    compounded = 1.0
    for f in folds:
        compounded *= 1 + f.test_metrics.net_return
    return {
        "folds": len(folds),
        "oos_compounded_return": compounded - 1,
        "oos_positive_folds": sum(1 for f in folds if f.test_metrics.net_return > 0),
        "oos_trades": sum(f.test_metrics.trades for f in folds),
        "oos_worst_drawdown": max(f.test_metrics.max_drawdown for f in folds),
        "oos_benchmark_compounded": math.prod(1 + (f.test_metrics.benchmark_return or 0) for f in folds) - 1,
    }
