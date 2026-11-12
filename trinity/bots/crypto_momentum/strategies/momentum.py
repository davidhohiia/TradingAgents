"""Momentum Trading sub-strategy.

Entry (completed bar t):
  close > EMA20 > EMA50 > EMA200 (aligned trend), MACD histogram > 0 and rising,
  RSI in [rsi_min, rsi_max] (strong but not exhausted), volume >= min ratio x average,
  volatility filter passed.
Stop = entry - k x ATR, take profit = entry + R x risk, trailing stop after activation.
Momentum exit: close < EMA20 AND MACD histogram < 0.
"""

from __future__ import annotations

import pandas as pd

from trinity.bots.crypto_momentum.signals import volatility_filter
from trinity.bots.crypto_momentum.strategies.base import EntrySignal, TradeState
from trinity.config.settings import MomentumIndicators, MomentumParams


class MomentumStrategy:
    name = "momentum"

    def __init__(self, params: MomentumParams, ind: MomentumIndicators) -> None:
        self.p = params
        self.ind = ind

    def entry(self, symbol: str, feat: pd.DataFrame) -> tuple[EntrySignal | None, list[str]]:
        if not self.p.enabled:
            return None, ["momentum disabled"]
        if len(feat) < 3:
            return None, ["insufficient data"]
        row, prev = feat.iloc[-1], feat.iloc[-2]
        p = self.p
        conds = [
            (row.close > row.ema_fast > row.ema_mid > row.ema_slow, "EMA20/50/200 not aligned up"),
            (row.macd_hist > 0, "MACD histogram <= 0"),
            (row.macd_hist > prev.macd_hist, "MACD histogram not rising"),
            (p.rsi_min <= row.rsi <= p.rsi_max, f"RSI {row.rsi:.1f} outside [{p.rsi_min}, {p.rsi_max}]"),
            (row.vol_ratio >= p.min_volume_ratio, f"volume ratio {row.vol_ratio:.2f} < {p.min_volume_ratio}"),
        ]
        vol_ok, vol_msg = volatility_filter(row, self.ind)
        conds.append((vol_ok, vol_msg))
        failed = [msg for ok, msg in conds if not ok]
        if failed:
            return None, failed
        entry = float(row.close)
        stop = entry - p.stop_atr_multiplier * float(row.atr)
        if stop <= 0:
            return None, ["invalid stop"]
        tp = entry + p.take_profit_r * (entry - stop)
        score = float(row.macd_hist / row.atr) + float(row.vol_ratio) * 0.1 + (row.close / row.ema_mid - 1) * 10
        return EntrySignal(self.name, symbol, entry, stop, tp, score,
                           [f"trend aligned, MACD hist {row.macd_hist:.4g} rising, RSI {row.rsi:.1f}, "
                            f"vol x{row.vol_ratio:.2f}, {vol_msg}"],
                           trailing_atr_multiplier=p.trailing_atr_multiplier,
                           trailing_activation_r=p.trailing_activation_r), []

    def exit_signal(self, feat: pd.DataFrame, state: TradeState) -> str | None:
        row = feat.iloc[-1]
        if row.close < row.ema_fast and row.macd_hist < 0:
            return "momentum lost (close < EMA20 and MACD histogram < 0)"
        return None
