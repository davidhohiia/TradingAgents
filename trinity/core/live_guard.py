"""Live-trading gate. LIVE mode is only possible if EVERY check passes.

1. LIVE_TRADING=true AND PAPER_TRADING=false (set by the user in .env)
2. LIVE_TRADING_ACKNOWLEDGEMENT equals the exact warning phrase
3. all required credentials present
4. full automated test suite passed on THIS exact code (hash) within the last 7 days
5. risk limits valid (no leverage/margin/short, sane limits)
6. venues/broker support the instruments and the account (checked asynchronously at start)
7. minimum paper-trading validation met (unless explicitly disabled in config)
8. no emergency / drawdown lock active
9. a separate interactive confirmation typed in the console at every live start
After a restart the system ALWAYS starts in paper mode unless started via run_live.py again.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from trinity.config.settings import (
    LIVE_ACKNOWLEDGEMENT_PHRASE,
    PROJECT_DIR,
    AppConfig,
    RiskConfig,
    Settings,
)
from trinity.core.models import Mode
from trinity.database.repository import Repository

TEST_RESULTS_PATH = PROJECT_DIR / "data" / "test_results.json"
TEST_RESULTS_MAX_AGE = timedelta(days=7)
INTERACTIVE_PHRASE = "START LIVE TRADING"


@dataclass
class GateCheck:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class LiveGateReport:
    checks: list[GateCheck] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(GateCheck(name, ok, detail))

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)

    @property
    def failed(self) -> list[GateCheck]:
        return [c for c in self.checks if not c.ok]

    def text(self) -> str:
        return "\n".join(f"[{'OK' if c.ok else 'FAIL'}] {c.name}: {c.detail}" for c in self.checks)


def source_hash(root: Path = PROJECT_DIR) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.suffix not in (".py", ".yaml", ".yml", ".sql") or any(part in ("data", "logs", "__pycache__") for part in p.parts):
            continue
        h.update(p.relative_to(root).as_posix().encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def read_test_results(path: Path = TEST_RESULTS_PATH) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def check_tests(now: datetime, path: Path = TEST_RESULTS_PATH, code_hash: str | None = None) -> GateCheck:
    res = read_test_results(path)
    if res is None:
        return GateCheck("automated_tests", False, "no test results: run `python -m pytest` in trinity/ (full suite)")
    code_hash = code_hash or source_hash()
    ts = datetime.fromisoformat(res["ts"])
    problems = []
    if not res.get("full_suite"):
        problems.append("last run was not the full suite")
    if res.get("failed", 1) or res.get("errors", 1):
        problems.append(f"{res.get('failed')} failed / {res.get('errors')} errors")
    if res.get("passed", 0) <= 0:
        problems.append("no passing tests recorded")
    if res.get("code_hash") != code_hash:
        problems.append("code changed since tests ran")
    if now - ts > TEST_RESULTS_MAX_AGE:
        problems.append("test results older than 7 days")
    return GateCheck("automated_tests", not problems,
                     "; ".join(problems) or f"{res['passed']} passed at {res['ts']}")


def check_risk_config(rc: RiskConfig) -> GateCheck:
    try:
        RiskConfig.model_validate(rc.model_dump())
    except Exception as exc:  # noqa: BLE001
        return GateCheck("risk_limits", False, str(exc))
    return GateCheck("risk_limits", True,
                     f"risk/trade {rc.max_risk_per_trade_pct:.2%}, daily {rc.max_daily_loss_pct:.0%}, "
                     f"weekly {rc.max_weekly_loss_pct:.0%}, live lock at DD {rc.drawdown_live_lock_pct:.0%}")


def evaluate_static_gate(settings: Settings, config: AppConfig, repo: Repository, now: datetime | None = None,
                         test_results: Path = TEST_RESULTS_PATH, code_hash: str | None = None) -> LiveGateReport:
    now = now or datetime.now(UTC)
    rep = LiveGateReport()
    rep.add("env_flags", settings.live_trading and not settings.paper_trading,
            f"LIVE_TRADING={settings.live_trading}, PAPER_TRADING={settings.paper_trading} (need true/false)")
    rep.add("acknowledgement", settings.live_trading_acknowledgement.strip() == LIVE_ACKNOWLEDGEMENT_PHRASE,
            "LIVE_TRADING_ACKNOWLEDGEMENT must equal the exact phrase from .env.example")
    s = config.strategies
    creds = []
    if s.crypto_momentum.enabled and not settings.has_crypto_credentials():
        creds.append("CRYPTO_API_KEY/CRYPTO_API_SECRET")
    if s.stock_trend.enabled and not settings.has_alpaca_credentials():
        creds.append("ALPACA_API_KEY_ID/ALPACA_API_SECRET_KEY")
    if s.stock_trend.enabled and settings.alpaca_paper_account:
        creds.append("ALPACA_PAPER_ACCOUNT must be false for real trading")
    rep.add("credentials", not creds, "missing: " + ", ".join(creds) if creds else "present")
    rep.checks.append(check_tests(now, test_results, code_hash))
    rep.checks.append(check_risk_config(config.risk))
    if config.paper.require_paper_validation:
        first = repo.first_equity_ts(Mode.PAPER)
        days = (now - first).days if first else 0
        st = repo.trade_stats(Mode.PAPER)
        crit = repo.query("SELECT COUNT(*) FROM risk_events WHERE mode='paper' AND event_type IN "
                          "('OVERFILL','LIMIT_VIOLATION','UNEXPECTED_FILL','RECONCILIATION_FAILED')")[0][0]
        ok = days >= config.paper.min_days and int(st["n"]) >= config.paper.min_closed_trades and crit == 0 \
            and float(st["pnl"]) > 0
        rep.add("paper_validation", ok,
                f"{days}/{config.paper.min_days} days, {int(st['n'])}/{config.paper.min_closed_trades} trades, "
                f"net {float(st['pnl']):.2f} USD, critical errors {crit}")
    else:
        rep.add("paper_validation", True, "DISABLED in config (not recommended)")
    raw = repo.get_kv("risk:live:state") or {}
    locked = [k for k in ("emergency_stop", "hard_stop", "drawdown_lock") if raw.get(k)]
    rep.add("risk_locks", not locked, "active: " + ", ".join(locked) if locked else "none")
    rep.add("leverage", True,
            "spot only at live start; leverage is evaluated separately by the LeverageGuard "
            "(>= 60 days of successful live trading without leverage + approval)")
    return rep
