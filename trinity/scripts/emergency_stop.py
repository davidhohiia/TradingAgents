"""Local risk-lock control. Works while the engine is running (state lives in the DB and is
read before EVERY order) and while it is stopped.

    python trinity/scripts/emergency_stop.py                   # ACTIVATE emergency stop (paper + live)
    python trinity/scripts/emergency_stop.py --status
    python trinity/scripts/emergency_stop.py --clear           # requires typed confirmation
    python trinity/scripts/emergency_stop.py --confirm-drawdown  # after reviewing a max-drawdown lock

Activating blocks every NEW position immediately (the flag is read before every order). If the
engine is running, its Position Guardian cancels unfilled entry orders within ~15 s and keeps
enforcing protective stops; exits stay allowed. Positions are never liquidated automatically.
Telegram /emergency_stop runs the full protocol at once (SPEC_4.0 A3).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trinity.config.settings import load_app_config, load_settings  # noqa: E402
from trinity.core.models import Mode  # noqa: E402
from trinity.database.repository import open_database  # noqa: E402
from trinity.risk.circuit_breaker import CircuitBreakerRegistry  # noqa: E402
from trinity.risk.risk_manager import DRAWDOWN_CONFIRM_PHRASE, HARD_STOP_CLEAR_PHRASE, RiskManager  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--status", action="store_true")
    g.add_argument("--clear", action="store_true")
    g.add_argument("--confirm-drawdown", action="store_true")
    g.add_argument("--clear-hard-stop", action="store_true")
    ap.add_argument("--mode", choices=["paper", "live", "both"], default="both")
    args = ap.parse_args()
    settings = load_settings()
    config = load_app_config()
    repo, _ = open_database(settings.database_path, settings.backup_dir)
    modes = [Mode.PAPER, Mode.LIVE] if args.mode == "both" else [Mode(args.mode)]
    breakers = CircuitBreakerRegistry(5, 60)
    try:
        for mode in modes:
            rm = RiskManager(config, repo, mode, breakers)
            if args.status:
                print(mode.value, "locks:", rm.blocking_locks() or "none", rm.state.reasons)
            elif args.clear:
                typed = input(f'[{mode.value}] Type "CLEAR EMERGENCY STOP": ').strip()
                print("cleared" if rm.clear_emergency_stop(typed) else "NOT cleared")
            elif args.confirm_drawdown:
                eq = repo.get_kv(f"risk:{mode.value}:equity_state") or {}
                print(f"[{mode.value}] peak {eq.get('peak')}, max DD {eq.get('max_dd')}")
                typed = input(f'Type "{DRAWDOWN_CONFIRM_PHRASE}" after reviewing positions and logs: ').strip()
                current = float(input("Current portfolio equity in USD (from your own review): ").strip() or 0)
                ok = current > 0 and rm.confirm_drawdown_review(typed, current)
                print("drawdown lock cleared (live stays disabled until run_live.py confirmation)" if ok else "NOT cleared")
            elif args.clear_hard_stop:
                typed = input(f'[{mode.value}] Type "{HARD_STOP_CLEAR_PHRASE}" after a full review: ').strip()
                current = float(input("Current portfolio equity in USD (from your own review): ").strip() or 0)
                ok = current > 0 and rm.clear_hard_stop(typed, current)
                print("hard stop cleared (live stays disabled until run_live.py confirmation)" if ok else "NOT cleared")
            else:
                rm.emergency_stop("local emergency_stop.py")
                print(f"[{mode.value}] EMERGENCY STOP ACTIVE: new orders blocked")
    finally:
        repo.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
