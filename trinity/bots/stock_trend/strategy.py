"""Stock trend-following decision logic (long only, no leverage, no options/CFD/margin)."""

from __future__ import annotations

from dataclasses import dataclass, field

from trinity.bots.stock_trend.signals import TrendSignals
from trinity.config.settings import StockTrendConfig
from trinity.core.models import MarketInfo
from trinity.risk.position_sizing import SizingResult, size_by_stop


@dataclass
class EntryDecision:
    enter: bool
    reasons: list[str] = field(default_factory=list)
    entry_price: float | None = None
    stop: float | None = None
    take_profit: float | None = None
    sizing: SizingResult | None = None


@dataclass
class ExitDecision:
    exit: bool
    reason: str = ""
    protective: bool = False
    new_trailing_stop: float | None = None


@dataclass
class PositionState:
    entry_price: float
    initial_stop: float
    take_profit: float
    highest_close: float
    trailing_stop: float | None = None


class StockTrendStrategy:
    def __init__(self, cfg: StockTrendConfig) -> None:
        self.cfg = cfg

    def evaluate_entry(
        self, sig: TrendSignals, *, bid: float, ask: float, market: MarketInfo, strategy_capital: float,
        risk_pct: float, available_cash: float, max_order_notional: float, max_position_notional: float,
        sell_fee_pct: float = 0.0,
    ) -> EntryDecision:
        c = self.cfg
        d = EntryDecision(False)
        if not sig.complete:
            d.reasons.extend(sig.reasons or ["indicators incomplete"])
            return d
        assert sig.ema_fast and sig.ema_slow and sig.rsi is not None and sig.atr and sig.close
        checks = [
            (sig.ema_fast > sig.ema_slow, f"EMA{c.ema_fast} {sig.ema_fast:.2f} <= EMA{c.ema_slow} {sig.ema_slow:.2f}"),
            (sig.close > sig.ema_fast, "close below fast EMA"),
            ((sig.ema_slow_slope or 0) > 0, "slow EMA not rising"),
            (c.rsi_min <= sig.rsi <= c.rsi_max, f"RSI {sig.rsi:.1f} outside [{c.rsi_min}, {c.rsi_max}]"),
            ((sig.up_down_volume_ratio or 0) >= c.min_up_down_volume_ratio,
             f"volume not confirming (up/down {sig.up_down_volume_ratio})"),
            ((sig.avg_dollar_volume or 0) >= c.min_avg_dollar_volume,
             f"liquidity {sig.avg_dollar_volume:,.0f} < {c.min_avg_dollar_volume:,.0f} USD/day"),
        ]
        spread_bps = (ask - bid) / ((ask + bid) / 2) * 10_000 if bid > 0 and ask > 0 else float("inf")
        checks.append((spread_bps <= c.max_spread_bps, f"spread {spread_bps:.1f}bps > {c.max_spread_bps}"))
        failed = [msg for ok, msg in checks if not ok]
        if failed:
            d.reasons.extend(failed)
            return d
        entry = ask * (1 + c.entry_limit_offset_bps / 10_000)
        stop = entry - c.stop_atr_multiplier * sig.atr
        if stop <= 0:
            d.reasons.append("invalid stop")
            return d
        risk_per_share = entry - stop
        tp = entry + c.take_profit_r * risk_per_share
        # expected return after costs must be positive and R:R acceptable
        round_trip_cost = entry * (market.taker_fee * 2 + sell_fee_pct) + (ask - bid)
        reward = tp - entry - round_trip_cost
        rr = reward / (risk_per_share + round_trip_cost)
        if reward <= 0 or rr < c.min_reward_risk:
            d.reasons.append(f"reward/risk after costs {rr:.2f} < {c.min_reward_risk}")
            return d
        sizing = size_by_stop(
            strategy_capital=strategy_capital, risk_pct=risk_pct, entry=entry, stop=stop,
            max_order_notional=max_order_notional, max_position_notional=max_position_notional,
            available_cash=available_cash, qty_step=market.qty_step, min_qty=market.min_qty,
            min_notional=market.min_notional, fee_rate=market.taker_fee,
        )
        if not sizing.ok:
            d.reasons.append(f"position size: {sizing.reason}"
                             + ("" if market.fractional else " (whole shares only at this broker)"))
            return d
        d.enter = True
        d.entry_price, d.stop, d.take_profit, d.sizing = entry, stop, tp, sizing
        d.reasons.append(
            f"EMA{c.ema_fast}>{c.ema_slow}, RSI {sig.rsi:.1f}, vol ratio {sig.up_down_volume_ratio:.2f}, "
            f"R:R {rr:.2f}, risk ${sizing.risk_usd:.2f}"
        )
        return d

    def evaluate_exit(self, sig: TrendSignals, ps: PositionState, *, bid: float) -> ExitDecision:
        c = self.cfg
        if sig.close is not None:
            ps.highest_close = max(ps.highest_close, sig.close)
        r = ps.entry_price - ps.initial_stop
        new_trail = None
        if sig.atr and r > 0 and ps.highest_close - ps.entry_price >= c.trailing_activation_r * r:
            candidate = ps.highest_close - c.trailing_atr_multiplier * sig.atr
            if ps.trailing_stop is None or candidate > ps.trailing_stop:
                new_trail = candidate
                ps.trailing_stop = candidate
        if bid <= ps.initial_stop:
            return ExitDecision(True, f"stop loss {ps.initial_stop:.2f} hit (bid {bid:.2f})", True, new_trail)
        if ps.trailing_stop is not None and bid <= ps.trailing_stop:
            return ExitDecision(True, f"trailing stop {ps.trailing_stop:.2f} hit (bid {bid:.2f})", True, new_trail)
        if bid >= ps.take_profit:
            return ExitDecision(True, f"take profit {ps.take_profit:.2f} reached", False, new_trail)
        if sig.trend_reversed:
            return ExitDecision(True, "trend reversal (EMA cross down or close below slow EMA)", True, new_trail)
        return ExitDecision(False, "", False, new_trail)
