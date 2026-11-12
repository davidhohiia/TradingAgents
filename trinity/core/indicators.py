"""Deterministic technical indicators (pandas). No look-ahead: value at t uses data <= t."""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
import pandas as pd

from trinity.core.models import Candle


def candles_to_df(candles: Sequence[Candle]) -> pd.DataFrame:
    df = pd.DataFrame(
        {
            "ts": [c.ts for c in candles],
            "open": [c.open for c in candles],
            "high": [c.high for c in candles],
            "low": [c.low for c in candles],
            "close": [c.close for c in candles],
            "volume": [c.volume for c in candles],
        }
    )
    if not df.empty:
        df = df.set_index("ts").sort_index()
        df = df[~df.index.duplicated(keep="last")]
    return df


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    out = out.where(avg_loss != 0, 100.0)
    return out.where(avg_gain.notna())


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr_s = true_range(df).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / tr_s
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / tr_s
    denom = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / denom
    return dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def realized_volatility(close: pd.Series, period: int = 20) -> pd.Series:
    return np.log(close).diff().rolling(period, min_periods=period).std()


def last_valid(series: pd.Series) -> float | None:
    s = series.dropna()
    if s.empty:
        return None
    v = float(s.iloc[-1])
    return v if math.isfinite(v) else None


def floor_to_step(value: float, step: float) -> float:
    """Round DOWN to a multiple of step without float artifacts."""
    if step <= 0:
        return value
    n = math.floor(value / step + 1e-9)
    decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(n * step, decimals + 2)


def round_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    n = round(value / step)
    decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(n * step, decimals + 2)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = close.ewm(span=fast, adjust=False, min_periods=fast).mean() - close.ewm(
        span=slow, adjust=False, min_periods=slow).mean()
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"macd": line, "macd_signal": sig, "macd_hist": line - sig})


def rolling_high(series: pd.Series, window: int) -> pd.Series:
    """Highest value of the PREVIOUS ``window`` bars (excludes the current bar: no look-ahead)."""
    return series.shift(1).rolling(window, min_periods=window).max()
