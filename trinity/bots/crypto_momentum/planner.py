"""Decision core of Crypto Momentum, shared by the live/paper bot and the backtester."""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from trinity.bots.crypto_momentum.signals import compute_features, features_ready
from trinity.bots.crypto_momentum.strategies.base import EntrySignal, ExitDecision, TradeState, manage_exit
from trinity.bots.crypto_momentum.strategies.breakout import BreakoutStrategy
from trinity.bots.crypto_momentum.strategies.momentum import MomentumStrategy
from trinity.config.settings import CryptoMomentumConfig
from trinity.core.indicators import floor_to_step
from trinity.core.models import MarketInfo
from trinity.risk.correlation import return_correlations, select_uncorrelated


@dataclass
class PlanNotes:
    per_symbol: dict[str, list[str]] = field(default_factory=dict)
    correlation: list[str] = field(default_factory=list)


class MomentumPlanner:
    def __init__(self, cfg: CryptoMomentumConfig, correlation_threshold: float = 0.8,
                 correlation_lookback: int = 120) -> None:
        self.cfg = cfg
        self.strategies = {
            "momentum": MomentumStrategy(cfg.momentum, cfg.indicators),
            "breakout": BreakoutStrategy(cfg.breakout, cfg.indicators),
        }
        self.threshold = correlation_threshold
        self.lookback = correlation_lookback

    def features(self, df: pd.DataFrame) -> pd.DataFrame:
        return compute_features(df, self.cfg.indicators)

    def ready(self, feat: pd.DataFrame) -> bool:
        return features_ready(feat, self.cfg.indicators)

    def correlations(self, feats: dict[str, pd.DataFrame]) -> dict[tuple[str, str], float]:
        return return_correlations({s: f["close"] for s, f in feats.items()}, self.lookback)

    def candidates(self, feats: dict[str, pd.DataFrame], skip: set[str]) -> tuple[list[EntrySignal], PlanNotes]:
        notes = PlanNotes()
        out: list[EntrySignal] = []
        for sym, feat in feats.items():
            if sym in skip:
                continue
            if not self.ready(feat):
                notes.per_symbol[sym] = ["indicators not ready (need EMA200 history)"]
                continue
            best: EntrySignal | None = None
            reasons: list[str] = []
            for name, strat in self.strategies.items():
                sig, why = strat.entry(sym, feat)
                if sig is not None and (best is None or sig.score > best.score):
                    best = sig
                elif sig is None:
                    reasons.append(f"{name}: {why[0] if why else 'no signal'}")
            if best is not None:
                out.append(best)
                notes.per_symbol[sym] = [f"{best.strategy}: " + "; ".join(best.reasons)]
            else:
                notes.per_symbol[sym] = reasons
        return out, notes

    def select(self, cands: list[EntrySignal], held: list[str], matrix: dict[tuple[str, str], float],
               notes: PlanNotes) -> list[EntrySignal]:
        """At most one risky position per highly-correlated cluster (e.g. BTC/ETH/SOL moving together)."""
        accepted, rej = select_uncorrelated([(c.symbol, c.score) for c in cands], held, matrix, self.threshold)
        notes.correlation.extend(rej)
        return [c for c in cands if c.symbol in accepted]

    def exit_decision(self, feat: pd.DataFrame, state: TradeState, bid: float) -> ExitDecision:
        row = feat.iloc[-1]
        atr_v = float(row.atr) if row.atr == row.atr else None
        d = manage_exit(state, float(row.close), atr_v, bid)
        if d.exit:
            return d
        strat = self.strategies.get(state.sub_strategy)
        if strat is not None:
            why = strat.exit_signal(feat, state)
            if why:
                return ExitDecision(True, why, True)  # risk-reducing exit
        return d

    @staticmethod
    def size(sig: EntrySignal, *, max_risk_usd: float, available: float, max_notional: float,
             market: MarketInfo, price: float, leverage: float = 1.0) -> float:
        """Risk-based size: qty = risk budget / (entry - stop); capped by notional and capital.

        Leverage never increases the risk budget; it only lowers the margin needed.
        """
        if sig.risk_per_unit <= 0 or price <= 0:
            return 0.0
        qty = max_risk_usd / sig.risk_per_unit
        qty = min(qty, max_notional / price, available * max(leverage, 1.0) / (price * (1 + market.taker_fee)))
        qty = floor_to_step(qty, market.qty_step)
        if qty < market.min_qty or qty * price < market.min_notional:
            return 0.0
        return qty
