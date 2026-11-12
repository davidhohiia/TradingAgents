"""Stock trend indicators: EMA20/EMA50, RSI14, ATR14, volume confirmation (pure, no look-ahead)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from trinity.config.settings import StockTrendConfig
from trinity.core.indicators import atr, ema, last_valid, rsi


@dataclass
class TrendSignals:
    close: float | None = None
    ema_fast: float | None = None
    ema_slow: float | None = None
    ema_slow_slope: float | None = None
    rsi: float | None = None
    atr: float | None = None
    up_down_volume_ratio: float | None = None
    avg_dollar_volume: float | None = None
    reasons: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return None not in (self.close, self.ema_fast, self.ema_slow, self.rsi, self.atr,
                            self.up_down_volume_ratio, self.avg_dollar_volume)

    @property
    def trend_up(self) -> bool:
        return bool(self.complete and self.ema_fast > self.ema_slow and self.close > self.ema_fast  # type: ignore[operator]
                    and (self.ema_slow_slope or 0) > 0)

    @property
    def trend_reversed(self) -> bool:
        return bool(self.complete and (self.ema_fast < self.ema_slow or self.close < self.ema_slow))  # type: ignore[operator]


def compute_signals(df: pd.DataFrame, cfg: StockTrendConfig) -> TrendSignals:
    sig = TrendSignals()
    need = cfg.ema_slow + 10
    if df is None or len(df) < need:
        sig.reasons.append(f"insufficient history: {0 if df is None else len(df)} < {need} bars")
        return sig
    close = df["close"]
    ef, es = ema(close, cfg.ema_fast), ema(close, cfg.ema_slow)
    sig.close = float(close.iloc[-1])
    sig.ema_fast = last_valid(ef)
    sig.ema_slow = last_valid(es)
    if len(es.dropna()) > 5:
        sig.ema_slow_slope = float(es.iloc[-1] / es.iloc[-6] - 1)
    sig.rsi = last_valid(rsi(close, cfg.rsi_period))
    sig.atr = last_valid(atr(df, cfg.atr_period))
    tail = df.iloc[-cfg.volume_lookback:]
    chg = tail["close"].diff()
    up_vol = float(tail["volume"][chg > 0].sum())
    down_vol = float(tail["volume"][chg < 0].sum())
    sig.up_down_volume_ratio = up_vol / down_vol if down_vol > 0 else (10.0 if up_vol > 0 else None)
    sig.avg_dollar_volume = float((tail["close"] * tail["volume"]).mean())
    return sig
