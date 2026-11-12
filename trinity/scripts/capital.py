"""Capital Growth Manager CLI (local only).

    python trinity/scripts/capital.py --status [--mode paper|live]
    python trinity/scripts/capital.py --deposit 200 [--note "June"] [--currency EUR --fx 1.08]
    python trinity/scripts/capital.py --propose [--mode paper|live]
    python trinity/scripts/capital.py --approve ID      # typed confirmation required
    python trinity/scripts/capital.py --reject ID

Deposits always go to the RESERVE. The reserve is never used automatically: capital moves
to a strategy only via a proposal that passes the risk analysis and an explicit approval.
In LIVE mode you must also transfer the money to the exchange/broker yourself.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trinity.config.settings import Settings, load_app_config, load_settings  # noqa: E402
from trinity.core.app import build_app  # noqa: E402
from trinity.core.capital_growth import APPROVAL_PHRASE  # noqa: E402
from trinity.core.models import Mode  # noqa: E402
from trinity.database.repository import open_database  # noqa: E402


def main(argv: list[str] | None = None, settings: Settings | None = None) -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--status", action="store_true")
    g.add_argument("--deposit", type=float)
    g.add_argument("--propose", action="store_true")
    g.add_argument("--approve", type=int)
    g.add_argument("--reject", type=int)
    ap.add_argument("--note", default="")
    ap.add_argument("--currency", default="USD", choices=["USD", "EUR"])
    ap.add_argument("--fx", type=float, default=1.0, help="EUR->USD rate for EUR deposits")
    ap.add_argument("--mode", choices=["paper", "live"], default="paper")
    args = ap.parse_args(argv)
    settings = settings or load_settings()
    config = load_app_config()
    repo, _ = open_database(settings.database_path, settings.backup_dir)
    try:
        app = build_app(settings, config, repo, Mode(args.mode), crypto_data=None, stock_data=None, arb_sources={})
        pm = app.pm
        pm.last_equity = app.engine.account_equity()
        pm.risk.on_equity(pm.last_equity)
        if args.deposit is not None:
            dep = pm.register_deposit(args.deposit, args.note, args.currency, args.fx)
            print(f"deposit #{dep} registered -> reserve now {pm.growth.reserve():.2f} USD "
                  "(not counted as profit; not allocated automatically)")
        elif args.propose:
            ids, notes = pm.propose_reallocation()
            print("created proposals:", ids or "none")
            for n in notes:
                print(" -", n)
        elif args.approve is not None:
            chk = pm.growth.check_proposal(args.approve, pm.risk.blocking_locks(),
                                           pm.risk.last_status.drawdown_pct if pm.risk.last_status else 0.0)
            if not chk.ok:
                print("cannot approve:", chk.reasons)
                return 1
            typed = input(f'Type "{APPROVAL_PHRASE}" to move capital from the reserve: ').strip()
            ok, reasons = pm.approve_reallocation(args.approve, typed)
            print("approved" if ok else f"NOT approved: {reasons}")
            if ok and args.mode == "live":
                print("LIVE: transfer the funds to the venue yourself; reconciliation will verify balances.")
        elif args.reject is not None:
            pm.growth.reject(args.reject, args.note)
            print("rejected")
        else:
            print(json.dumps(pm.capital_data(), indent=2, default=str))
        app.repo.close()
    finally:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
