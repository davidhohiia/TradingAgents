"""Deterministic market regime filter (ranging / bull / bear / strong bear / high volatility)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from trinity.config.settings import RegimeParams
from trinity.core.indicators import adx, atr, ema, last_valid, rsi
from trinity.core.models import MarketRegime


@dataclass
class RegimeResult:
    regime: MarketRegime
    close: float | None = None
    atr: float | None = None
    atr_pct: float | None = None
    rsi: float | None = None
    adx: float | None = None
    ema_slope_pct: float | None = None
    ema_trend: float | None = None
    reasons: list[str] = field(default_factory=list)

    @property
    def sufficient_data(self) -> bool:
        return self.regime != MarketRegime.UNKNOWN

    def as_dict(self) -> dict[str, object]:
        return {
            "regime": self.regime.value, "close": self.close, "atr": self.atr, "atr_pct": self.atr_pct,
            "rsi": self.rsi, "adx": self.adx, "ema_slope_pct": self.ema_slope_pct, "reasons": self.reasons,
        }


def classify_regime(df: pd.DataFrame, p: RegimeParams, atr_period: int = 14) -> RegimeResult:
    need = p.trend_period + p.slope_lookback + 2
    if df is None or len(df) < need:
        return RegimeResult(MarketRegime.UNKNOWN, reasons=[f"insufficient data: {0 if df is None else len(df)} < {need} bars"])
    close = df["close"]
    ema_t = ema(close, p.trend_period)
    last_close = float(close.iloc[-1])
    e_now = last_valid(ema_t)
    e_prev = float(ema_t.iloc[-1 - p.slope_lookback]) if len(ema_t) > p.slope_lookback else None
    a = last_valid(atr(df, atr_period))
    r = last_valid(rsi(close, 14))
    x = last_valid(adx(df, p.adx_period))
    if e_now is None or e_prev is None or a is None or x is None or e_prev != e_prev:
        return RegimeResult(MarketRegime.UNKNOWN, reasons=["indicators not available yet"])
    slope = e_now / e_prev - 1
    atr_pct = a / last_close
    res = RegimeResult(MarketRegime.RANGING, last_close, a, atr_pct, r, x, slope, e_now)
    if atr_pct >= p.high_vol_atr_pct:
        res.regime = MarketRegime.HIGH_VOLATILITY
        res.reasons.append(f"ATR {atr_pct:.2%} of price >= {p.high_vol_atr_pct:.2%}")
    elif slope <= p.strong_bear_slope_pct and last_close < e_now:
        res.regime = MarketRegime.STRONG_BEAR
        res.reasons.append(
            f"EMA{p.trend_period} slope {slope:.2%} over {p.slope_lookback} bars <= {p.strong_bear_slope_pct:.2%}"
            f" and close below EMA"
        )
    elif x >= p.adx_trend_threshold and last_close > e_now and slope > 0:
        res.regime = MarketRegime.BULL
        res.reasons.append(f"ADX {x:.1f} >= {p.adx_trend_threshold}, close above rising EMA{p.trend_period}")
    elif x >= p.adx_trend_threshold and last_close < e_now and slope < 0:
        res.regime = MarketRegime.BEAR
        res.reasons.append(f"ADX {x:.1f} >= {p.adx_trend_threshold}, close below falling EMA{p.trend_period}")
    else:
        res.reasons.append(f"ADX {x:.1f} < {p.adx_trend_threshold} or no aligned trend: ranging")
    return res
