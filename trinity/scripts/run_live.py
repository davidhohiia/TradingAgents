"""Start TRINITY in LIVE mode (real money). Every start goes through all gates again.

    python trinity/scripts/run_live.py            # gated start
    python trinity/scripts/run_live.py --check    # only show the gate report
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trinity.config.settings import (  # noqa: E402
    AppConfig,
    Settings,
    load_app_config,
    load_settings,
    secret_value,
)
from trinity.core.live_guard import (  # noqa: E402
    INTERACTIVE_PHRASE,
    LiveGateReport,
    evaluate_static_gate,
)
from trinity.core.models import Mode  # noqa: E402
from trinity.database.repository import open_database  # noqa: E402

WARNING = """
==========================================================================
 LIVE TRADING WARNING
 * Real money will be used. You can lose part or all of the allocated capital.
 * No profit is guaranteed. Paper results do not predict live results.
 * API keys must have TRADE permission only. Withdrawals MUST be disabled.
 * Keep the computer awake and online; outages can prevent stop-loss exits.
==========================================================================
"""


async def verify_venues(settings: Settings, config: AppConfig) -> LiveGateReport:
    from trinity.exchanges.crypto_exchange import CCXTExchange
    from trinity.exchanges.stock_broker import AlpacaBroker

    rep = LiveGateReport()
    s = config.strategies
    if s.crypto_momentum.enabled:
        ex = CCXTExchange(settings.crypto_exchange_id, api_key=secret_value(settings.crypto_api_key),
                          api_secret=secret_value(settings.crypto_api_secret),
                          api_password=secret_value(settings.crypto_api_password))
        try:
            await ex.connect()
            missing = [sym for sym in s.crypto_momentum.symbols if not ex.supports(sym)]
            await ex.fetch_balances()  # proves the key works (read permission)
            rep.add("crypto_exchange", not missing,
                    f"{settings.crypto_exchange_id}: " + (f"missing spot markets {missing}" if missing else "symbols OK"))
        except Exception as exc:  # noqa: BLE001
            rep.add("crypto_exchange", False, f"{type(exc).__name__}: {str(exc)[:200]}")
        finally:
            await ex.close()
    if s.stock_trend.enabled:
        try:
            broker = AlpacaBroker(secret_value(settings.alpaca_api_key_id), secret_value(settings.alpaca_api_secret_key),
                                  paper_account=settings.alpaca_paper_account)
            cap = await broker.verify_capabilities(s.stock_trend.symbols)
            rep.add("stock_broker", cap.ok, "; ".join(cap.problems) or f"instruments {cap.instruments}")
            await broker.close()
        except Exception as exc:  # noqa: BLE001
            rep.add("stock_broker", False, f"{type(exc).__name__}: {str(exc)[:200]}")
    rep.add("arbitrage", True, "scan-only (virtual); never trades live")
    return rep


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args, rest = ap.parse_known_args()
    settings = load_settings()
    config = load_app_config()
    repo, notes = open_database(settings.database_path, settings.backup_dir)
    try:
        report = evaluate_static_gate(settings, config, repo)
    finally:
        repo.close()
    print(report.text())
    if notes:
        print("database recovery notes:", notes)
    if not report.ok:
        print("\nLIVE TRADING NOT ALLOWED. Fix the failed checks above. The system can run in paper mode:"
              " python trinity/scripts/run_paper.py")
        return 1
    venue_rep = asyncio.run(verify_venues(settings, config))
    print(venue_rep.text())
    if not venue_rep.ok:
        print("\nLIVE TRADING NOT ALLOWED: venue/broker verification failed.")
        return 1
    if args.check:
        return 0
    if not sys.stdin.isatty():
        print("Live start requires an interactive console (no automatic live start).")
        return 1
    print(WARNING)
    typed = input(f'Type "{INTERACTIVE_PHRASE}" to start live trading: ').strip()
    if typed != INTERACTIVE_PHRASE:
        print("Confirmation not given. Aborting.")
        return 1
    repo, _ = open_database(settings.database_path, settings.backup_dir)
    raw = repo.get_kv("risk:live:state") or {}
    if raw.get("live_disabled") and not raw.get("drawdown_lock") and not raw.get("emergency_stop"):
        raw["live_disabled"] = False
        raw.get("reasons", {}).pop("live_disabled", None)
        repo.set_kv("risk:live:state", raw)
        repo.add_risk_event(Mode.LIVE, "WARNING", "LIVE_REENABLED", "live re-enabled by interactive confirmation")
    repo.close()
    from trinity.main import main as run_main

    return run_main(rest, _live_authorized=True)


if __name__ == "__main__":
    raise SystemExit(main())
