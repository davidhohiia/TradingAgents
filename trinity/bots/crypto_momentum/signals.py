"""Feature computation for Crypto Momentum (EMA 20/50/200, RSI, MACD, ATR, volume, volatility).

All features at bar t use only bars <= t (verified by backtesting/lookahead_check.py).
"""

from __future__ import annotations

import pandas as pd

from trinity.config.settings import MomentumIndicators
from trinity.core.indicators import atr, ema, macd, rsi, sma


def compute_features(df: pd.DataFrame, p: MomentumIndicators) -> pd.DataFrame:
    out = df.copy()
    c = out["close"]
    out["ema_fast"] = ema(c, p.ema_fast)
    out["ema_mid"] = ema(c, p.ema_mid)
    out["ema_slow"] = ema(c, p.ema_slow)
    out["rsi"] = rsi(c, p.rsi_period)
    m = macd(c, p.macd_fast, p.macd_slow, p.macd_signal)
    out["macd"], out["macd_signal"], out["macd_hist"] = m["macd"], m["macd_signal"], m["macd_hist"]
    out["atr"] = atr(out, p.atr_period)
    out["atr_pct"] = out["atr"] / c
    out["vol_ratio"] = out["volume"] / sma(out["volume"], p.volume_lookback)
    return out


def volatility_filter(row: pd.Series, p: MomentumIndicators) -> tuple[bool, str]:
    a = row.get("atr_pct")
    if a is None or a != a:
        return False, "ATR unavailable"
    if a < p.min_atr_pct:
        return False, f"volatility too low (ATR {a:.2%} < {p.min_atr_pct:.2%})"
    if a > p.max_atr_pct:
        return False, f"volatility too high (ATR {a:.2%} > {p.max_atr_pct:.2%})"
    return True, f"ATR {a:.2%}"


def features_ready(feat: pd.DataFrame, p: MomentumIndicators) -> bool:
    if len(feat) < p.ema_slow + 5:
        return False
    last = feat.iloc[-1]
    return all(last[k] == last[k] for k in ("ema_slow", "rsi", "macd_hist", "atr", "vol_ratio"))
