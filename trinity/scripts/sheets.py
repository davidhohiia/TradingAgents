"""Google Sheets trading journal CLI.

    python trinity/scripts/sheets.py --check            # credentials + spreadsheet access (creates sheets/headers)
    python trinity/scripts/sheets.py --sync             # send pending trades now + rebuild statistic sheets
    python trinity/scripts/sheets.py --status           # pending/sent counters, last success/error
    python trinity/scripts/sheets.py --errors           # sync error log
    python trinity/scripts/sheets.py --oauth-setup      # one-time OAuth 2.0 consent (writes GOOGLE_OAUTH_TOKEN_FILE)
Options: --mode paper|live (which statistics to publish; default paper)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trinity.config.settings import load_app_config, load_settings  # noqa: E402
from trinity.core.app import build_app  # noqa: E402
from trinity.core.models import Mode  # noqa: E402
from trinity.database.repository import open_database  # noqa: E402
from trinity.integrations.sheets.client import SCOPES  # noqa: E402


def oauth_setup(client_secret: Path, token_file: Path) -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
    creds = flow.run_local_server(port=0)
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(creds.to_json(), encoding="utf-8")
    try:
        token_file.chmod(0o600)
    except OSError:
        pass
    print(f"OAuth token saved to {token_file} (keep it private, never commit it)")


async def run(args: argparse.Namespace) -> int:
    settings, config = load_settings(), load_app_config()
    if args.oauth_setup:
        if not settings.google_oauth_client_secret_file or not settings.google_oauth_token_file:
            print("set GOOGLE_OAUTH_CLIENT_SECRET_FILE and GOOGLE_OAUTH_TOKEN_FILE in .env first")
            return 1
        oauth_setup(settings.google_oauth_client_secret_file, settings.google_oauth_token_file)
        return 0
    repo, _ = open_database(settings.database_path, settings.backup_dir)
    try:
        app = build_app(settings, config, repo, Mode(args.mode), arb_sources={})
        pm = app.pm
        if args.errors:
            for r in repo.list_sync_errors(50):
                print(r["ts"], r["operation"], r["error"])
            return 0
        if args.status:
            st = pm.sheets.status() if pm.sheets else {**repo.outbox_stats(), "configured": False}
            print(json.dumps(st, indent=2, default=str))
            return 0
        if pm.sheets is None:
            print("Google Sheets journal not enabled/configured (see README: Google Sheets).",
                  "Pending trades kept locally:", repo.outbox_stats()["pending"])
            return 1
        pm.last_equity = app.engine.account_equity()
        pm.risk.on_equity(pm.last_equity)
        ok, msg = await pm.sheets.check_connection()
        print("connection:", "OK" if ok else "FAILED", msg)
        if args.sync and ok:
            res = await pm.sheets.sync_all()
            print(f"sync: {'OK' if res.ok else 'FAILED'} appended={res.appended} "
                  f"already_present={res.already_present} pending_left={res.pending_left} {res.errors}")
            ok = res.ok
        await app.close()
        return 0 if ok else 1
    finally:
        repo.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    for flag in ("--check", "--sync", "--status", "--errors", "--oauth-setup"):
        g.add_argument(flag, action="store_true")
    ap.add_argument("--mode", choices=["paper", "live"], default="paper")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
