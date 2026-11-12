"""Shared types and exit management for Crypto Momentum sub-strategies."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class EntrySignal:
    strategy: str
    symbol: str
    entry: float
    stop: float
    take_profit: float
    score: float
    reasons: list[str] = field(default_factory=list)
    level: float | None = None
    trailing_atr_multiplier: float = 2.5
    trailing_activation_r: float = 1.0

    @property
    def risk_per_unit(self) -> float:
        return self.entry - self.stop


@dataclass
class TradeState:
    """Per-position exit state (persisted)."""

    sub_strategy: str
    entry: float
    initial_stop: float
    take_profit: float
    highest_close: float
    trailing_atr_multiplier: float
    trailing_activation_r: float
    level: float | None = None
    trailing_stop: float | None = None
    bars_held: int = 0
    last_bar: str | None = None

    @property
    def active_stop(self) -> float:
        return max(self.initial_stop, self.trailing_stop or 0.0)


@dataclass
class ExitDecision:
    exit: bool
    reason: str = ""
    protective: bool = False


def manage_exit(state: TradeState, close: float, atr_value: float | None, bid: float) -> ExitDecision:
    """Stop / trailing stop / take profit. Updates the trailing stop (never lowers it)."""
    state.highest_close = max(state.highest_close, close)
    r = state.entry - state.initial_stop
    if atr_value and r > 0 and state.highest_close - state.entry >= state.trailing_activation_r * r:
        cand = state.highest_close - state.trailing_atr_multiplier * atr_value
        if state.trailing_stop is None or cand > state.trailing_stop:
            state.trailing_stop = cand
    if bid <= state.initial_stop:
        return ExitDecision(True, f"stop loss {state.initial_stop:.2f} hit (bid {bid:.2f})", True)
    if state.trailing_stop is not None and bid <= state.trailing_stop:
        return ExitDecision(True, f"trailing stop {state.trailing_stop:.2f} hit (bid {bid:.2f})", True)
    if bid >= state.take_profit:
        return ExitDecision(True, f"take profit {state.take_profit:.2f} reached", False)
    return ExitDecision(False)
