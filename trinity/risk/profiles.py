"""Named risk profiles. BALANCED is the only approved profile (SPEC_4.0.md, section A4).

The profile values are constants in code. The YAML configuration must match them exactly or it
will not load; per-bot Trade Quality thresholds may only be RAISED above the profile minimum.
At runtime the Risk Engine re-verifies the profile before every order (tamper detection).
No AI module, Telegram command or automation has a code path that changes these values.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class RiskProfile:
    name: str
    max_risk_per_trade_pct: float
    max_total_open_risk_pct: float
    max_daily_loss_pct: float
    max_weekly_loss_pct: float
    drawdown_warning_pct: float
    drawdown_reduce_pct: float
    drawdown_live_lock_pct: float
    drawdown_emergency_pct: float
    min_trade_quality_score: int

    def fingerprint(self) -> str:
        return hashlib.sha256(repr(sorted(asdict(self).items())).encode()).hexdigest()[:16]


BALANCED = RiskProfile(
    name="BALANCED",
    max_risk_per_trade_pct=0.0075,
    max_total_open_risk_pct=0.03,
    max_daily_loss_pct=0.03,
    max_weekly_loss_pct=0.06,
    drawdown_warning_pct=0.10,
    drawdown_reduce_pct=0.12,
    drawdown_live_lock_pct=0.15,
    drawdown_emergency_pct=0.20,
    min_trade_quality_score=70,
)

PROFILES: dict[str, RiskProfile] = {"BALANCED": BALANCED}
RISK_FIELDS = ("max_risk_per_trade_pct", "max_total_open_risk_pct", "max_daily_loss_pct", "max_weekly_loss_pct",
               "drawdown_warning_pct", "drawdown_reduce_pct", "drawdown_live_lock_pct", "drawdown_emergency_pct")


def get_profile(name: str) -> RiskProfile:
    if name not in PROFILES:
        raise ValueError(f"unknown risk profile '{name}' (only {', '.join(PROFILES)} is approved)")
    return PROFILES[name]


def profile_violations(profile: RiskProfile, risk: Any, quality: Any) -> list[str]:
    """Differences between the live configuration objects and the profile (empty list = intact)."""
    out = []
    for f in RISK_FIELDS:
        cur = getattr(risk, f)
        if abs(float(cur) - float(getattr(profile, f))) > 1e-12:
            out.append(f"{f}={cur} (profile {profile.name}: {getattr(profile, f)})")
    if int(quality.min_score) != profile.min_trade_quality_score:
        out.append(f"min_trade_quality_score={quality.min_score} (profile: {profile.min_trade_quality_score})")
    for bot, thr in dict(quality.per_bot).items():
        if int(thr) < profile.min_trade_quality_score:
            out.append(f"quality threshold for {bot}={thr} below profile minimum {profile.min_trade_quality_score}")
    return out
