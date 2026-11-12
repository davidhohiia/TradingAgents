"""Verification that no future data leaks into past decisions.

Two independent checks:

1. ``check_feature_causality``: every feature value at bar t computed on the FULL
   history must equal the value computed on the history truncated at t. A feature that
   uses future bars (e.g. ``shift(-1)``, centered windows, full-sample normalization)
   fails this check.

2. ``check_backtest_causality``: run the backtest on the original data and on a copy
   whose bars AFTER a cut-off are randomly perturbed. Every fill up to the cut-off must be
   identical; otherwise a decision depended on data that was not yet available.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from trinity.backtesting.engine import BacktestConfig, BacktestEngine, BacktestStrategy


@dataclass
class LookaheadReport:
    ok: bool
    checked_points: int
    violations: list[str] = field(default_factory=list)


def check_feature_causality(df: pd.DataFrame, feature_fn: Callable[[pd.DataFrame], pd.DataFrame | pd.Series],
                            points: int = 25, min_bars: int = 50, tol: float = 1e-9) -> LookaheadReport:
    full = feature_fn(df)
    if isinstance(full, pd.Series):
        full = full.to_frame("value")
    idx = np.linspace(min_bars, len(df) - 2, num=min(points, len(df) - min_bars - 1), dtype=int)
    violations = []
    for t in idx:
        part = feature_fn(df.iloc[: t + 1])
        if isinstance(part, pd.Series):
            part = part.to_frame("value")
        for col in part.columns:
            if col not in full.columns:
                continue
            a, b = full[col].iloc[t], part[col].iloc[-1]
            both_nan = pd.isna(a) and pd.isna(b)
            if not both_nan and (pd.isna(a) != pd.isna(b) or abs(float(a) - float(b)) > tol * max(1.0, abs(float(a)))):
                violations.append(f"{col} at bar {t}: full-history value {a} != point-in-time value {b}")
    return LookaheadReport(not violations, len(idx), violations[:20])


def perturb_after(df: pd.DataFrame, cut: int, seed: int = 0, scale: float = 0.05) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    out = df.copy()
    n = len(df) - cut - 1
    if n <= 0:
        return out
    factor = np.exp(np.cumsum(rng.normal(0, scale, n)))
    for col in ("open", "high", "low", "close"):
        out.iloc[cut + 1:, out.columns.get_loc(col)] = df[col].iloc[cut + 1:].to_numpy() * factor
    out.iloc[cut + 1:, out.columns.get_loc("high")] = out[["open", "high", "low", "close"]].iloc[cut + 1:].max(axis=1)
    out.iloc[cut + 1:, out.columns.get_loc("low")] = out[["open", "high", "low", "close"]].iloc[cut + 1:].min(axis=1)
    out.iloc[cut + 1:, out.columns.get_loc("volume")] = df["volume"].iloc[cut + 1:].to_numpy() * rng.uniform(
        0.2, 3.0, n)
    return out


def check_backtest_causality(df: pd.DataFrame, make_strategy: Callable[[], BacktestStrategy], cfg: BacktestConfig,
                             start_index: int, cuts: list[int] | None = None) -> LookaheadReport:
    cuts = cuts or [start_index + (len(df) - start_index) * k // 4 for k in (1, 2, 3)]
    base = BacktestEngine(cfg).run(df, make_strategy(), start_index)
    violations = []
    for i, cut in enumerate(cuts):
        alt = BacktestEngine(cfg).run(perturb_after(df, cut, seed=i), make_strategy(), start_index)
        # fills on bars <= cut come from decisions on bars < cut: they must be identical
        a = [(f.index, f.side, round(f.qty, 10), round(f.price, 8)) for f in base.fills if f.index <= cut]
        b = [(f.index, f.side, round(f.qty, 10), round(f.price, 8)) for f in alt.fills if f.index <= cut]
        if a != b:
            violations.append(f"fills up to bar {cut} changed when only later bars were modified")
        if base.equity[: cut - start_index + 1] != alt.equity[: cut - start_index + 1]:
            violations.append(f"equity up to bar {cut} changed when only later bars were modified")
    return LookaheadReport(not violations, len(cuts), violations)
