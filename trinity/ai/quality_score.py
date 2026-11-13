"""AI Trade Quality Score (0..100).

Deterministic scoring of a candidate entry from REAL market data only. A factor whose input is
missing scores 0 and is reported as missing - nothing is estimated or invented.

Factors (max points):  trend strength/ADX 15 | EMA direction 15 | RSI 10 | MACD 10 | volume 10 |
ATR/volatility 10 | liquidity (spread, depth) 10 | market regime 10 | reward/risk 5 | correlation 5.

Bands: 0-39 weak | 40-59 low | 60-74 acceptable | 75-89 strong | 90-100 very strong.
The score is an additional filter: an entry needs score >= the bot threshold (BALANCED minimum 70)
AND must still pass every Risk Engine check. The score can never relax a risk limit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MAX_POINTS = {"trend": 15, "ema": 15, "rsi": 10, "macd": 10, "volume": 10, "volatility": 10,
              "liquidity": 10, "regime": 10, "reward_risk": 5, "correlation": 5}


@dataclass
class QualityInputs:
    adx: float | None = None
    ema_aligned: bool | None = None  # fast > mid (> slow) for longs
    ema_slope_pct: float | None = None  # slope of the slow/mid EMA
    rsi: float | None = None
    macd_hist: float | None = None
    macd_rising: bool | None = None
    volume_ratio: float | None = None
    atr_pct: float | None = None
    atr_band: tuple[float, float] = (0.004, 0.05)
    spread_bps: float | None = None
    depth_ratio: float | None = None  # top-of-book (or 5-level) liquidity / order size
    regime: str | None = None
    reward_risk: float | None = None
    max_correlation: float | None = None  # with currently held positions (None = nothing held)
    held_positions: int = 0


@dataclass
class QualityScore:
    score: int
    grade: str
    components: dict[str, float] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def passes(self, threshold: int) -> bool:
        return self.score >= threshold

    def as_dict(self) -> dict[str, Any]:
        return {"score": self.score, "grade": self.grade, "components": self.components, "missing": self.missing}


def grade_of(score: float) -> str:
    if score >= 90:
        return "very strong"
    if score >= 75:
        return "strong"
    if score >= 60:
        return "acceptable"
    if score >= 40:
        return "low"
    return "weak"


def _lin(x: float, lo: float, hi: float) -> float:
    if hi == lo:
        return 1.0 if x >= hi else 0.0
    return min(max((x - lo) / (hi - lo), 0.0), 1.0)


def score_trade(q: QualityInputs) -> QualityScore:
    c: dict[str, float] = {}
    missing: list[str] = []

    def need(name: str, *vals: Any) -> bool:
        if any(v is None for v in vals):
            missing.append(name)
            c[name] = 0.0
            return False
        return True

    if need("trend", q.adx):
        c["trend"] = MAX_POINTS["trend"] * _lin(q.adx, 15, 30)  # type: ignore[arg-type]
    if need("ema", q.ema_aligned, q.ema_slope_pct):
        c["ema"] = (10.0 if q.ema_aligned else 0.0) + (5.0 if (q.ema_slope_pct or 0) > 0 else 0.0)
    if need("rsi", q.rsi):
        r = q.rsi
        assert r is not None
        c["rsi"] = 10.0 if 55 <= r <= 68 else 6.0 if (50 <= r < 55 or 68 < r <= 72) else 2.0 if 45 <= r < 50 else 0.0
    if need("macd", q.macd_hist, q.macd_rising):
        c["macd"] = (5.0 if (q.macd_hist or 0) > 0 else 0.0) + (5.0 if q.macd_rising else 0.0)
    if need("volume", q.volume_ratio):
        v = q.volume_ratio
        assert v is not None
        c["volume"] = 10.0 if v >= 1.5 else 7.0 if v >= 1.1 else 4.0 if v >= 0.8 else 0.0
    if need("volatility", q.atr_pct):
        lo, hi = q.atr_band
        a = q.atr_pct
        assert a is not None
        if a < lo or a > hi:
            c["volatility"] = 0.0
        else:
            mid_lo, mid_hi = lo + (hi - lo) * 0.15, hi - (hi - lo) * 0.4
            c["volatility"] = 10.0 if mid_lo <= a <= mid_hi else 5.0
    if need("liquidity", q.spread_bps):
        sp = q.spread_bps
        assert sp is not None
        spread_pts = 6.0 if sp <= 5 else 4.0 if sp <= 10 else 2.0 if sp <= 20 else 0.0
        depth_pts = 0.0 if q.depth_ratio is None else 4.0 if q.depth_ratio >= 5 else 2.0 if q.depth_ratio >= 2 else 0.0
        if q.depth_ratio is None:
            missing.append("depth")
        c["liquidity"] = spread_pts + depth_pts
    if need("regime", q.regime):
        c["regime"] = {"bull": 10.0, "ranging": 5.0, "high_volatility": 2.0}.get(str(q.regime), 0.0)
    if need("reward_risk", q.reward_risk):
        rr = q.reward_risk
        assert rr is not None
        c["reward_risk"] = 5.0 if rr >= 3 else 4.0 if rr >= 2 else 2.0 if rr >= 1.5 else 0.0
    if q.held_positions == 0:
        c["correlation"] = 5.0  # nothing held: no overlap
    elif need("correlation", q.max_correlation):
        mc = q.max_correlation
        assert mc is not None
        c["correlation"] = 5.0 if mc < 0.5 else 2.0 if mc < 0.8 else 0.0
    total = int(round(sum(c.values())))
    reasons = [f"{k} {v:.0f}/{MAX_POINTS[k]}" for k, v in c.items()]
    if missing:
        reasons.append("missing data: " + ", ".join(sorted(set(missing))))
    return QualityScore(total, grade_of(total), {k: round(v, 2) for k, v in c.items()}, sorted(set(missing)), reasons)
