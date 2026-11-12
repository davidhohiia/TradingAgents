"""Market Analyzer: regime classification + human-readable explanation of signals.

Deterministic indicators are the primary evidence. An optional external LLM may
be plugged in ONLY to rephrase the explanation; the system works fully without it,
and nothing an LLM returns can change orders, parameters or risk limits.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import pandas as pd

from trinity.config.settings import RegimeParams
from trinity.core.log import get_logger
from trinity.core.market_regime import RegimeResult, classify_regime
from trinity.core.models import MarketRegime

log = get_logger(__name__)

Explainer = Callable[[str], Awaitable[str]]


@dataclass
class MarketAnalysis:
    symbol: str
    regime: MarketRegime
    evidence: dict[str, float | None]
    explanation: str
    data_points: int
    warnings: list[str] = field(default_factory=list)


class MarketAnalyzer:
    def __init__(self, params: RegimeParams | None = None, explainer: Explainer | None = None) -> None:
        self.params = params or RegimeParams()
        self.explainer = explainer

    def analyze(self, symbol: str, df: pd.DataFrame | None) -> MarketAnalysis:
        if df is None or df.empty:
            return MarketAnalysis(symbol, MarketRegime.UNKNOWN, {}, "No market data available: no analysis made.", 0,
                                  ["missing data"])
        r: RegimeResult = classify_regime(df, self.params)
        evidence = {"close": r.close, "atr_pct": r.atr_pct, "rsi": r.rsi, "adx": r.adx,
                    "ema_slope_pct": r.ema_slope_pct}
        text = self._explain(symbol, r)
        warnings = [] if r.sufficient_data else ["insufficient data for a reliable classification"]
        return MarketAnalysis(symbol, r.regime, evidence, text, len(df), warnings)

    @staticmethod
    def _explain(symbol: str, r: RegimeResult) -> str:
        if r.regime == MarketRegime.UNKNOWN:
            return f"{symbol}: regime unknown ({'; '.join(r.reasons)})."
        parts = [f"{symbol}: {r.regime.value.replace('_', ' ')}"]
        if r.adx is not None:
            parts.append(f"ADX {r.adx:.1f} ({'trending' if r.adx >= 25 else 'weak trend'})")
        if r.ema_slope_pct is not None:
            parts.append(f"trend EMA slope {r.ema_slope_pct:+.2%}")
        if r.atr_pct is not None:
            parts.append(f"ATR {r.atr_pct:.2%} of price")
        if r.rsi is not None:
            parts.append(f"RSI {r.rsi:.1f}")
        return ", ".join(parts) + ". Basis: " + "; ".join(r.reasons)

    async def explain_with_llm(self, analysis: MarketAnalysis) -> str:
        """Optional rephrasing. Falls back to the deterministic text on any error."""
        if self.explainer is None:
            return analysis.explanation
        try:
            return await self.explainer(analysis.explanation)
        except Exception as exc:  # noqa: BLE001
            log.warning("external explainer unavailable, using deterministic explanation: %s", exc)
            return analysis.explanation
