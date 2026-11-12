"""Limited-leverage permission (local only). Leverage is OFF by default.

    python trinity/scripts/leverage.py --status
    python trinity/scripts/leverage.py --approve     # only possible if ALL gates pass
    python trinity/scripts/leverage.py --revoke

Gates: LIVE_LEVERAGE_ENABLED=true, LEVERAGE_ACKNOWLEDGEMENT phrase, leverage.enabled=true,
>= 60 days of successful live trading without leverage, no critical live errors, full test
suite passed on this code. Approval freezes the maximum (<= 2x hard cap); raising it later
requires a new approval - the system never increases leverage on its own.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trinity.config.settings import load_app_config, load_settings  # noqa: E402
from trinity.core.live_guard import check_tests  # noqa: E402
from trinity.core.models import Mode  # noqa: E402
from trinity.database.repository import open_database  # noqa: E402
from trinity.risk.leverage import APPROVAL_PHRASE, LeverageGuard  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--status", action="store_true")
    g.add_argument("--approve", action="store_true")
    g.add_argument("--revoke", action="store_true")
    args = ap.parse_args()
    settings, config = load_settings(), load_app_config()
    repo, _ = open_database(settings.database_path, settings.backup_dir)
    guard = LeverageGuard(config.leverage, settings, repo, Mode.LIVE)
    now = datetime.now(UTC)
    tests_ok = check_tests(now).ok
    try:
        if args.revoke:
            guard.revoke(now, "revoked locally")
            print("leverage approval revoked")
            return 0
        if args.approve:
            if not sys.stdin.isatty():
                print("interactive console required")
                return 1
            if not tests_ok:
                print("full automated test suite must pass on this code first")
                return 1
            typed = input(f'Type "{APPROVAL_PHRASE}": ').strip()
            rep = guard.record_approval(typed, now)
        else:
            rep = guard.permission(now, tests_ok)
        print("live history:", guard.live_history(now))
        print("PERMITTED" if rep.permitted else "NOT PERMITTED")
        for r in rep.reasons:
            print(" -", r)
        return 0 if rep.permitted else 1
    finally:
        repo.close()


if __name__ == "__main__":
    raise SystemExit(main())
