"""Stock breakout detection (Donchian-style, confirmed by close and volume).

Level = highest high of the ``lookback`` completed bars BEFORE the last completed bar.
Signal: last completed close > level, volume >= ratio x average, close > slow EMA,
price not extended more than ``max_extension_atr`` x ATR above the level.
"""

from __future__ import annotations

import pandas as pd

from trinity.bots.stock_trend.signals import TrendSignals
from trinity.bots.stock_trend.strategy import EntryDecision
from trinity.config.settings import StockTrendConfig
from trinity.core.indicators import sma
from trinity.core.models import MarketInfo
from trinity.risk.position_sizing import size_by_stop


def detect_breakout(df: pd.DataFrame, cfg: StockTrendConfig) -> tuple[float | None, list[str]]:
    p = cfg.breakout
    if df is None or len(df) < p.lookback_bars + 2:
        return None, ["insufficient history for breakout"]
    level = float(df["high"].iloc[-1 - p.lookback_bars:-1].max())
    last = df.iloc[-1]
    vol_avg = sma(df["volume"], cfg.volume_lookback).iloc[-2]
    ratio = float(last.volume / vol_avg) if vol_avg and vol_avg == vol_avg else 0.0
    reasons = []
    if last.close <= level:
        reasons.append(f"no close above {p.lookback_bars}-day high {level:.2f}")
    if ratio < p.min_volume_ratio:
        reasons.append(f"breakout volume x{ratio:.2f} < {p.min_volume_ratio}")
    return (level if not reasons else None), reasons


def evaluate_breakout_entry(
    df: pd.DataFrame, sig: TrendSignals, cfg: StockTrendConfig, *, bid: float, ask: float, market: MarketInfo,
    strategy_capital: float, risk_pct: float, available_cash: float, max_order_notional: float,
    max_position_notional: float,
) -> EntryDecision:
    d = EntryDecision(False)
    p = cfg.breakout
    if not p.enabled:
        d.reasons.append("breakout disabled")
        return d
    if not sig.complete:
        d.reasons.extend(sig.reasons or ["indicators incomplete"])
        return d
    level, why = detect_breakout(df, cfg)
    if level is None:
        d.reasons.extend(why)
        return d
    assert sig.atr and sig.ema_slow and sig.close
    if sig.close <= sig.ema_slow:
        d.reasons.append("close below slow EMA (trend filter)")
        return d
    if sig.close - level > p.max_extension_atr * sig.atr:
        d.reasons.append("price too extended above the breakout level")
        return d
    spread_bps = (ask - bid) / ((ask + bid) / 2) * 10_000 if bid > 0 and ask > 0 else float("inf")
    if spread_bps > cfg.max_spread_bps:
        d.reasons.append(f"spread {spread_bps:.1f}bps > {cfg.max_spread_bps}")
        return d
    entry = ask * (1 + cfg.entry_limit_offset_bps / 10_000)
    stop = level - p.stop_atr_below_level * sig.atr
    if stop <= 0 or stop >= entry:
        d.reasons.append("invalid stop")
        return d
    tp = entry + p.take_profit_r * (entry - stop)
    sizing = size_by_stop(strategy_capital=strategy_capital, risk_pct=risk_pct, entry=entry, stop=stop,
                          max_order_notional=max_order_notional, max_position_notional=max_position_notional,
                          available_cash=available_cash, qty_step=market.qty_step, min_qty=market.min_qty,
                          min_notional=market.min_notional, fee_rate=market.taker_fee)
    if not sizing.ok:
        d.reasons.append(f"position size: {sizing.reason}")
        return d
    d.enter, d.entry_price, d.stop, d.take_profit, d.sizing = True, entry, stop, tp, sizing
    d.reasons.append(f"breakout over {level:.2f}, risk ${sizing.risk_usd:.2f}")
    return d
