"""Correlation analysis: assets (to avoid duplicate risky positions) and strategies."""

from __future__ import annotations

import itertools
import math
from collections.abc import Iterable

import numpy as np
import pandas as pd


def return_correlations(closes: dict[str, pd.Series], lookback: int = 120) -> dict[tuple[str, str], float]:
    """Pearson correlation of log returns over the last ``lookback`` common bars."""
    frame = pd.DataFrame({k: v for k, v in closes.items() if v is not None and len(v) > 2})
    if frame.shape[1] < 2:
        return {}
    rets = np.log(frame).diff().dropna().tail(lookback)
    out: dict[tuple[str, str], float] = {}
    if len(rets) < 20:
        return out
    corr = rets.corr()
    for a, b in itertools.combinations(corr.columns, 2):
        v = float(corr.loc[a, b])
        if math.isfinite(v):
            out[(a, b)] = v
            out[(b, a)] = v
    return out


def correlated_with(symbol: str, held: Iterable[str], matrix: dict[tuple[str, str], float],
                    threshold: float) -> list[tuple[str, float]]:
    return [(h, matrix[(symbol, h)]) for h in held
            if h != symbol and (symbol, h) in matrix and matrix[(symbol, h)] >= threshold]


def select_uncorrelated(candidates: list[tuple[str, float]], held: Iterable[str],
                        matrix: dict[tuple[str, str], float], threshold: float) -> tuple[list[str], list[str]]:
    """Greedy choice of at most one position per highly correlated cluster.

    candidates: (symbol, score). Already-held symbols occupy their cluster.
    Returns (accepted symbols, rejection notes).
    """
    chosen: list[str] = list(held)
    accepted, notes = [], []
    for sym, _score in sorted(candidates, key=lambda x: -x[1]):
        clash = correlated_with(sym, chosen, matrix, threshold)
        if sym in chosen:
            notes.append(f"{sym}: position already open")
        elif clash:
            notes.append(f"{sym}: correlated with {clash[0][0]} ({clash[0][1]:.2f}) - duplicate risk skipped")
        else:
            accepted.append(sym)
            chosen.append(sym)
    return accepted, notes


def strategy_correlation(daily_pnl: dict[str, pd.Series]) -> dict[tuple[str, str], float]:
    """Correlation between strategies' daily PnL series (needs >= 10 overlapping days)."""
    frame = pd.DataFrame(daily_pnl).fillna(0.0)
    out: dict[tuple[str, str], float] = {}
    if frame.shape[1] < 2 or len(frame) < 10:
        return out
    corr = frame.corr()
    for a, b in itertools.combinations(corr.columns, 2):
        v = float(corr.loc[a, b])
        if math.isfinite(v):
            out[(a, b)] = v
    return out
