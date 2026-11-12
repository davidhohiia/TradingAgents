"""Limited leverage: permission gate + per-order liquidation / funding protection.

Leverage is OFF by default (LIVE_LEVERAGE_ENABLED=false, leverage.enabled=false) and is
never used in paper mode or in the initial phase of real trading. Permission requires ALL of:

1. LIVE_LEVERAGE_ENABLED=true and LEVERAGE_ACKNOWLEDGEMENT phrase in .env;
2. leverage.enabled=true in strategies.yaml (isolated margin, BTC/ETH only, <= 2x hard cap);
3. >= 60 days of real (live) trading WITHOUT leverage, net positive, no critical execution errors;
4. full automated test suite passed on the current code;
5. a local approval record written by scripts/leverage.py (typed confirmation), which also
   freezes the approved maximum - the system can never raise leverage automatically.

Per-order checks (only when permitted): leverage <= min(config max, approved max, 2.0),
symbol allowed, isolated margin, liquidation price computed BEFORE the order, the protective
stop must sit well above the liquidation price, funding rate within the configured limit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from trinity.config.settings import (
    ABSOLUTE_MAX_LEVERAGE,
    LEVERAGE_ACKNOWLEDGEMENT_PHRASE,
    LeverageConfig,
    Settings,
)
from trinity.core.models import AssetClass, Mode, OrderIntent, Side
from trinity.database.repository import Repository

APPROVAL_KEY = "leverage:approval"
APPROVAL_PHRASE = "APPROVE LIMITED LEVERAGE"
CRITICAL_EVENTS = ("OVERFILL", "LIMIT_VIOLATION", "UNEXPECTED_FILL", "RECONCILIATION_FAILED", "LIQUIDATION_RISK")


def liquidation_price_long(entry: float, leverage: float, mmr: float) -> float:
    """Isolated-margin linear contract, long: approx. entry * (1 - 1/L + mmr)."""
    if leverage <= 1.0:
        return 0.0  # unleveraged spot cannot be liquidated
    return entry * (1.0 - 1.0 / leverage + mmr)


def liquidation_price_short(entry: float, leverage: float, mmr: float) -> float:
    if leverage <= 1.0:
        return float("inf")
    return entry * (1.0 + 1.0 / leverage - mmr)


def funding_cost(notional: float, rate_8h: float, hours: float) -> float:
    """Expected funding paid by a LONG over ``hours`` (positive rate = longs pay)."""
    return notional * rate_8h * hours / 8.0


@dataclass
class PermissionReport:
    permitted: bool
    reasons: list[str] = field(default_factory=list)
    approved_max: float = 1.0


class LeverageGuard:
    def __init__(self, cfg: LeverageConfig, settings: Settings | None, repo: Repository, mode: Mode) -> None:
        self.cfg = cfg
        self.settings = settings
        self.repo = repo
        self.mode = mode

    # ------------------------------------------------------------------ permission
    def live_history(self, now: datetime) -> dict[str, Any]:
        first = self.repo.query("SELECT MIN(ts) FROM equity_history WHERE mode='live'")[0][0]
        days = (now - datetime.fromisoformat(first)).days if first else 0
        st = self.repo.trade_stats(Mode.LIVE)
        levered = self.repo.query("SELECT COUNT(*) FROM orders WHERE mode='live' AND leverage>1")[0][0]
        crit = self.repo.query(
            f"SELECT COUNT(*) FROM risk_events WHERE mode='live' AND event_type IN ({','.join('?' * len(CRITICAL_EVENTS))})",
            CRITICAL_EVENTS)[0][0]
        return {"days": days, "trades": int(st["n"]), "net_pnl": float(st["pnl"]), "levered_orders": int(levered),
                "critical_errors": int(crit)}

    def permission(self, now: datetime, tests_ok: bool | None = None) -> PermissionReport:
        r: list[str] = []
        if self.mode != Mode.LIVE:
            r.append("leverage is never used in paper mode")
        if not self.cfg.enabled:
            r.append("leverage.enabled=false in strategies.yaml")
        s = self.settings
        if s is None or not s.live_leverage_enabled:
            r.append("LIVE_LEVERAGE_ENABLED is not true")
        if s is None or s.leverage_acknowledgement.strip() != LEVERAGE_ACKNOWLEDGEMENT_PHRASE:
            r.append("LEVERAGE_ACKNOWLEDGEMENT phrase missing")
        h = self.live_history(now)
        if h["days"] < self.cfg.required_live_days_without_leverage:
            r.append(f"only {h['days']} days of live trading (need {self.cfg.required_live_days_without_leverage})")
        if h["net_pnl"] <= 0:
            r.append(f"live trading not successful (net {h['net_pnl']:.2f} USD)")
        if h["critical_errors"]:
            r.append(f"{h['critical_errors']} critical live execution errors")
        if tests_ok is False:
            r.append("automated tests not passed on current code")
        approval = self.repo.get_kv(APPROVAL_KEY)
        approved_max = 1.0
        if not approval:
            r.append("no local approval record (scripts/leverage.py --approve)")
        else:
            approved_max = float(approval.get("max_leverage", 1.0))
            if self.cfg.max_leverage > approved_max + 1e-9:
                r.append(f"configured max {self.cfg.max_leverage}x above approved {approved_max}x "
                         "(leverage cannot be increased automatically)")
        return PermissionReport(not r, r, approved_max)

    def record_approval(self, confirmation: str, now: datetime) -> PermissionReport:
        """Called by the local CLI only. Writes the frozen maximum if every other gate passes."""
        if confirmation != APPROVAL_PHRASE:
            return PermissionReport(False, ["confirmation phrase mismatch"])
        pre = self.permission(now)
        blocking = [x for x in pre.reasons if not x.startswith("no local approval")]
        if blocking:
            return PermissionReport(False, blocking)
        self.repo.set_kv(APPROVAL_KEY, {"max_leverage": min(self.cfg.max_leverage, ABSOLUTE_MAX_LEVERAGE),
                                        "ts": now.isoformat(), "valid_until": (now + timedelta(days=90)).isoformat()})
        self.repo.add_risk_event(Mode.LIVE, "WARNING", "LEVERAGE_APPROVED",
                                 f"limited leverage approved up to {self.cfg.max_leverage}x", ts=now)
        return self.permission(now)

    def revoke(self, now: datetime, reason: str) -> None:
        self.repo.delete_kv(APPROVAL_KEY)
        self.repo.add_risk_event(self.mode, "WARNING", "LEVERAGE_REVOKED", reason, ts=now)

    # ------------------------------------------------------------------ per order
    def check_intent(self, intent: OrderIntent, now: datetime, funding_rate_8h: float | None,
                     tests_ok: bool | None = None) -> list[str]:
        if intent.leverage <= 1.0:
            return []
        reasons: list[str] = []
        perm = self.permission(now, tests_ok)
        if not perm.permitted:
            return [f"leverage not permitted: {'; '.join(perm.reasons)}"]
        cap = min(self.cfg.max_leverage, perm.approved_max, ABSOLUTE_MAX_LEVERAGE)
        if intent.leverage > cap + 1e-9:
            reasons.append(f"leverage {intent.leverage}x > allowed {cap}x")
        if intent.asset_class != AssetClass.CRYPTO or intent.symbol not in self.cfg.symbols:
            reasons.append(f"leverage not allowed for {intent.symbol}")
        if intent.venue != self.cfg.venue:
            reasons.append(f"leveraged orders must go to the isolated-margin venue {self.cfg.venue}")
        if intent.side == Side.BUY:
            entry = intent.effective_price
            liq = liquidation_price_long(entry, intent.leverage, self.cfg.maintenance_margin_rate)
            if intent.stop_price is None:
                reasons.append("leveraged order requires a stop loss")
            else:
                min_stop = liq + (entry - liq) * self.cfg.min_stop_to_liquidation_buffer_pct
                if intent.stop_price <= min_stop:
                    reasons.append(f"stop {intent.stop_price:.2f} too close to liquidation {liq:.2f} "
                                   f"(must be > {min_stop:.2f})")
            if funding_rate_8h is None:
                reasons.append("funding rate unknown: leveraged entry refused")
            elif funding_rate_8h > self.cfg.max_funding_rate_8h:
                reasons.append(f"funding rate {funding_rate_8h:.4%}/8h above limit {self.cfg.max_funding_rate_8h:.4%}")
        return reasons
