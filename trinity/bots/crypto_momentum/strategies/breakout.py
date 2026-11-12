"""Confirmed Breakout sub-strategy.

Level = highest high of the ``lookback`` bars BEFORE the breakout bar (no look-ahead).
Breakout bar: close > level, volume >= min ratio x average, close in the upper part of
its range (not just a wick), trend filter close > EMA200, volatility filter passed.
Confirmation: the following ``confirmation_bars`` bars all CLOSE above the level.
Skip if price already extended more than ``max_extension_atr`` x ATR above the level.
Stop = level - k x ATR (back inside the range = failed breakout); TP = R multiple.
Exit: close back below the level (failed breakout) or stop/trailing/TP.
"""

from __future__ import annotations

import pandas as pd

from trinity.bots.crypto_momentum.signals import volatility_filter
from trinity.bots.crypto_momentum.strategies.base import EntrySignal, TradeState
from trinity.config.settings import BreakoutParams, MomentumIndicators


class BreakoutStrategy:
    name = "breakout"

    def __init__(self, params: BreakoutParams, ind: MomentumIndicators) -> None:
        self.p = params
        self.ind = ind

    def entry(self, symbol: str, feat: pd.DataFrame) -> tuple[EntrySignal | None, list[str]]:
        p = self.p
        if not p.enabled:
            return None, ["breakout disabled"]
        k = p.confirmation_bars
        need = p.lookback_bars + k + 2
        if len(feat) < need:
            return None, ["insufficient data"]
        b_idx = len(feat) - 1 - k  # breakout bar
        window = feat.iloc[b_idx - p.lookback_bars: b_idx]
        level = float(window["high"].max())
        bbar = feat.iloc[b_idx]
        last = feat.iloc[-1]
        rng = float(bbar.high - bbar.low)
        close_pos = (bbar.close - bbar.low) / rng if rng > 0 else 0.0
        conds = [
            (bbar.close > level, f"no close above {p.lookback_bars}-bar high {level:.2f}"),
            (feat.iloc[b_idx - 1].close <= level, "breakout is not fresh (previous bar already above level)"),
            (bbar.vol_ratio >= p.min_volume_ratio, f"breakout volume x{bbar.vol_ratio:.2f} < {p.min_volume_ratio}"),
            (close_pos >= p.min_close_position_in_range, f"weak breakout close ({close_pos:.0%} of range)"),
            (last.close > last.ema_slow, "below EMA200 (trend filter)"),
            (all(feat.iloc[b_idx + 1 + i].close > level for i in range(k)), "breakout not confirmed by close"),
            ((last.close - level) <= p.max_extension_atr * last.atr, "price too extended above the level"),
        ]
        vol_ok, vol_msg = volatility_filter(last, self.ind)
        conds.append((vol_ok, vol_msg))
        failed = [msg for ok, msg in conds if not ok]
        if failed:
            return None, failed
        entry = float(last.close)
        stop = level - p.stop_atr_below_level * float(last.atr)
        if stop <= 0 or stop >= entry:
            return None, ["invalid stop"]
        tp = entry + p.take_profit_r * (entry - stop)
        score = float(bbar.vol_ratio) * 0.2 + float((last.close - level) / last.atr)
        return EntrySignal(self.name, symbol, entry, stop, tp, score,
                           [f"confirmed breakout over {level:.2f} (vol x{bbar.vol_ratio:.2f}, close {close_pos:.0%} "
                            f"of range, {k} confirming close(s)), {vol_msg}"],
                           level=level, trailing_atr_multiplier=p.trailing_atr_multiplier,
                           trailing_activation_r=p.trailing_activation_r), []

    def exit_signal(self, feat: pd.DataFrame, state: TradeState) -> str | None:
        if state.level is not None and feat.iloc[-1].close < state.level:
            return f"failed breakout (close back below {state.level:.2f})"
        return None
