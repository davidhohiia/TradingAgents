"""Synthetic OHLCV builders for tests (clearly artificial data)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

T0 = datetime(2026, 3, 2, tzinfo=UTC)


def make_df(prices: list[float], spread: float = 0.002, minutes: int = 60, volume=100.0) -> pd.DataFrame:
    idx = [T0 + timedelta(minutes=minutes * i) for i in range(len(prices))]
    rows = []
    prev = prices[0]
    vols = volume if isinstance(volume, list | np.ndarray) else [volume] * len(prices)
    for p, v in zip(prices, vols, strict=True):
        rows.append({"open": prev, "high": max(prev, p) * (1 + spread), "low": min(prev, p) * (1 - spread),
                     "close": p, "volume": float(v)})
        prev = p
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx))


def ranging(n=300, base=60000.0, amp=0.01):
    return [base * (1 + amp * math.sin(i / 6)) for i in range(n)]


def crash(n=300, base=60000.0, step=-0.004):
    return [base * (1 + step) ** i for i in range(n)]


def uptrend(n=300, base=60000.0, drift=0.002, noise=0.004, seed=1):
    rng = np.random.default_rng(seed)
    return list(base * np.cumprod(1 + drift + rng.normal(0, noise, n)))
