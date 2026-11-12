"""Capital Growth Manager.

* registers every contribution (initial capital and monthly deposits) - deposits are
  NEVER counted as trading income;
* separates contributed capital from trading profit (fees and real losses included,
  because equity is computed from net realized/unrealized PnL);
* every deposit goes to the RESERVE; the reserve is never used automatically;
* reallocation from the reserve happens only through a proposal that passes the risk
  analysis AND an explicit local approval (typed confirmation);
* monthly returns use the Modified Dietz method so deposits do not inflate returns;
* the monthly "target" is informational only and never influences trading.
"""

from __future__ import annotations

import calendar
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from trinity.config.settings import AppConfig
from trinity.core.clock import Clock, RealClock
from trinity.core.log import get_logger
from trinity.database.models import str_to_ts
from trinity.database.repository import Repository

log = get_logger(__name__)

ALLOC_KEY = "capital:allocations"
APPROVAL_PHRASE = "APPROVE REALLOCATION"
MIN_TRADES_FOR_REALLOCATION = 30


@dataclass
class ProposalCheck:
    ok: bool
    reasons: list[str] = field(default_factory=list)


class CapitalGrowthManager:
    def __init__(self, repo: Repository, config: AppConfig, clock: Clock | None = None) -> None:
        self.repo = repo
        self.config = config
        self.clock = clock or RealClock()
        self.ensure_initialized()

    # ------------------------------------------------------------------ state
    def ensure_initialized(self) -> None:
        if self.repo.get_kv(ALLOC_KEY) is not None:
            return
        cap = self.config.capital
        now = self.clock.now()
        with self.repo.transaction():
            if not self.repo.list_deposits():
                self.repo.add_deposit(now, cap.total_capital_usd, "initial", "initial capital")
            state = {"strategies": dict(cap.allocations), "reserve": cap.reserve_usd,
                     "unallocated": cap.total_capital_usd - sum(cap.allocations.values()) - cap.reserve_usd}
            self.repo.set_kv(ALLOC_KEY, state)
            for sid, amount in cap.allocations.items():
                self.repo.add_allocation_change(now, sid, amount, amount, "initial allocation", "config")
            self.repo.add_allocation_change(now, "reserve", cap.reserve_usd, cap.reserve_usd, "initial reserve",
                                            "config")

    def _state(self) -> dict[str, Any]:
        return self.repo.get_kv(ALLOC_KEY) or {"strategies": {}, "reserve": 0.0}

    def allocations(self) -> dict[str, float]:
        return {k: float(v) for k, v in self._state()["strategies"].items()}

    def allocation(self, strategy_id: str) -> float:
        return float(self._state()["strategies"].get(strategy_id, 0.0))

    def reserve(self) -> float:
        return float(self._state()["reserve"])

    def contributed(self) -> float:
        return float(sum(r["amount_usd"] for r in self.repo.list_deposits()))

    # ------------------------------------------------------------------ deposits
    def register_deposit(self, amount: float, *, kind: str = "monthly", note: str = "", currency: str = "USD",
                         fx_rate: float = 1.0, ts: datetime | None = None) -> int:
        if amount <= 0:
            raise ValueError("deposit must be positive")
        if kind not in ("monthly", "other"):
            raise ValueError("kind must be 'monthly' or 'other'")
        if currency != "USD" and fx_rate <= 0:
            raise ValueError("fx_rate required for non-USD deposits")
        usd = amount * (fx_rate if currency != "USD" else 1.0)
        ts = ts or self.clock.now()
        with self.repo.transaction():
            dep_id = self.repo.add_deposit(ts, usd, kind, note, currency, amount, fx_rate if currency != "USD" else 1.0)
            st = self._state()
            st["reserve"] = float(st["reserve"]) + usd
            self.repo.set_kv(ALLOC_KEY, st)
            self.repo.add_allocation_change(ts, "reserve", usd, st["reserve"], f"deposit #{dep_id}", "deposit")
            # Deposits raise equity but are NOT performance: shift drawdown/daily/weekly baselines.
            for mode in ("paper", "live"):
                key = f"risk:{mode}:equity_state"
                eq = self.repo.get_kv(key)
                if eq:
                    for k in ("peak", "day_start", "week_start"):
                        if k in eq:
                            eq[k] = float(eq[k]) + usd
                    self.repo.set_kv(key, eq)
            self.repo.add_system_event("INFO", "capital", "deposit", f"deposit {usd:.2f} USD -> reserve",
                                       {"id": dep_id, "kind": kind}, ts=ts)
        log.info("deposit registered: %.2f USD (reserve now %.2f)", usd, self.reserve())
        return dep_id

    # ------------------------------------------------------------------ reallocation
    def propose(self, performance: dict[str, dict[str, Any]], risk_locks: list[str], drawdown_pct: float,
                degraded: set[str]) -> tuple[list[int], list[str]]:
        """Create PENDING proposals (never applied automatically). Returns (ids, notes)."""
        notes: list[str] = []
        if risk_locks:
            return [], [f"no proposals while risk locks are active: {risk_locks}"]
        if drawdown_pct >= self.config.risk.drawdown_warning_pct:
            return [], [f"no proposals during drawdown {drawdown_pct:.1%}"]
        reserve = self.reserve()
        if reserve <= 0:
            return [], ["reserve is empty"]
        cap_limit = self.config.capital.max_strategy_allocation_pct * self.contributed()
        ids = []
        budget = reserve * 0.5  # never propose more than half of the reserve in one round
        for sid, perf in performance.items():
            if sid == "arbitrage":
                notes.append("arbitrage: scan-only, not eligible for capital")
                continue
            trades = int(perf.get("trades", 0))
            pf = perf.get("profit_factor")
            net = float(perf.get("net_pnl", 0.0))
            if trades < MIN_TRADES_FOR_REALLOCATION:
                notes.append(f"{sid}: only {trades} live/paper trades (< {MIN_TRADES_FOR_REALLOCATION})")
                continue
            if sid in degraded:
                notes.append(f"{sid}: degradation detected")
                continue
            if net <= 0 or pf is None or pf < 1.2:
                notes.append(f"{sid}: insufficient edge (net {net:.2f}, PF {pf})")
                continue
            room = cap_limit - self.allocation(sid)
            amount = round(min(budget, room, self.config.capital.monthly_contribution_usd), 2)
            if amount < 10:
                notes.append(f"{sid}: no room under the {self.config.capital.max_strategy_allocation_pct:.0%} cap")
                continue
            cur = self.repo.execute(
                "INSERT INTO reallocation_proposals (ts, strategy_id, amount_usd, rationale, evidence_json, status) "
                "VALUES (?,?,?,?,?, 'pending')",
                (self.clock.now().isoformat(), sid, amount,
                 f"{trades} trades, PF {pf:.2f}, net {net:.2f} USD; reserve {reserve:.2f}",
                 json.dumps(perf, default=str)))
            ids.append(int(cur.lastrowid))
            budget -= amount
        return ids, notes

    def pending(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.repo.query(
            "SELECT * FROM reallocation_proposals WHERE status='pending' ORDER BY id")]

    def check_proposal(self, proposal_id: int, risk_locks: list[str], drawdown_pct: float) -> ProposalCheck:
        rows = self.repo.query("SELECT * FROM reallocation_proposals WHERE id=?", (proposal_id,))
        if not rows:
            return ProposalCheck(False, ["proposal not found"])
        p = rows[0]
        reasons = []
        if p["status"] != "pending":
            reasons.append(f"proposal is {p['status']}")
        if p["amount_usd"] > self.reserve() + 1e-9:
            reasons.append("amount exceeds reserve")
        cap_limit = self.config.capital.max_strategy_allocation_pct * self.contributed()
        if self.allocation(p["strategy_id"]) + p["amount_usd"] > cap_limit + 1e-9:
            reasons.append("allocation would exceed the per-strategy cap")
        if risk_locks:
            reasons.append(f"risk locks active: {risk_locks}")
        if drawdown_pct >= self.config.risk.drawdown_warning_pct:
            reasons.append(f"drawdown {drawdown_pct:.1%} >= warning level")
        if p["strategy_id"] == "arbitrage":
            reasons.append("arbitrage cannot receive capital (scan-only)")
        return ProposalCheck(not reasons, reasons)

    def approve(self, proposal_id: int, confirmation: str, risk_locks: list[str], drawdown_pct: float,
                approved_by: str = "local-operator") -> ProposalCheck:
        if confirmation != APPROVAL_PHRASE:
            return ProposalCheck(False, ["confirmation phrase mismatch"])
        chk = self.check_proposal(proposal_id, risk_locks, drawdown_pct)
        if not chk.ok:
            return chk
        p = self.repo.query("SELECT * FROM reallocation_proposals WHERE id=?", (proposal_id,))[0]
        now = self.clock.now()
        with self.repo.transaction():
            st = self._state()
            st["reserve"] = float(st["reserve"]) - p["amount_usd"]
            st["strategies"][p["strategy_id"]] = float(st["strategies"].get(p["strategy_id"], 0.0)) + p["amount_usd"]
            self.repo.set_kv(ALLOC_KEY, st)
            self.repo.add_allocation_change(now, p["strategy_id"], p["amount_usd"],
                                            st["strategies"][p["strategy_id"]], f"proposal #{proposal_id}", approved_by)
            self.repo.add_allocation_change(now, "reserve", -p["amount_usd"], st["reserve"],
                                            f"proposal #{proposal_id}", approved_by)
            self.repo.execute("UPDATE reallocation_proposals SET status='approved', decided_at=? WHERE id=?",
                              (now.isoformat(), proposal_id))
        return ProposalCheck(True, [])

    def reject(self, proposal_id: int, note: str = "") -> None:
        self.repo.execute("UPDATE reallocation_proposals SET status='rejected', decided_at=?, decision_note=? "
                          "WHERE id=? AND status='pending'", (self.clock.now().isoformat(), note, proposal_id))

    # ------------------------------------------------------------------ performance
    def summary(self, equity: float, fees_paid: float) -> dict[str, Any]:
        contributed = self.contributed()
        profit = equity - contributed
        return {
            "contributed": contributed, "equity": equity, "trading_profit": profit,
            "return_on_contributed": profit / contributed if contributed else 0.0,
            "fees_paid": fees_paid, "reserve": self.reserve(), "allocations": self.allocations(),
            "deposits": [{"ts": r["ts"], "amount_usd": r["amount_usd"], "kind": r["kind"]}
                         for r in self.repo.list_deposits()],
            "informational_monthly_target_pct": self.config.capital.informational_monthly_target_pct,
        }

    def monthly_returns(self, equity_points: list[tuple[datetime, float]]) -> list[dict[str, Any]]:
        """Modified Dietz monthly returns: deposits are cash flows, not performance."""
        if not equity_points:
            return []
        deposits = [(str_to_ts(r["ts"]), float(r["amount_usd"])) for r in self.repo.list_deposits()
                    if r["kind"] != "initial"]
        months: dict[str, list[tuple[datetime, float]]] = {}
        for ts, eq in sorted(equity_points):
            months.setdefault(ts.strftime("%Y-%m"), []).append((ts, eq))
        out = []
        prev_end: float | None = None
        for month, pts in months.items():
            y, m = map(int, month.split("-"))
            days = calendar.monthrange(y, m)[1]
            start_dt = datetime(y, m, 1, tzinfo=UTC)
            flows = [(ts, amt) for ts, amt in deposits if ts and ts.strftime("%Y-%m") == month]
            v0 = prev_end if prev_end is not None else pts[0][1] - sum(a for ts, a in flows if ts <= pts[0][0])
            v1 = pts[-1][1]
            f = sum(a for _, a in flows)
            weighted = sum(a * (days - (ts - start_dt).total_seconds() / 86400) / days for ts, a in flows)
            denom = v0 + weighted
            r = (v1 - v0 - f) / denom if denom > 0 else 0.0
            out.append({"month": month, "start_equity": v0, "end_equity": v1, "deposits": f,
                        "return": r, "profit": v1 - v0 - f,
                        "target_informational": self.config.capital.informational_monthly_target_pct})
            prev_end = v1
        return out
