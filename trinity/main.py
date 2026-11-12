"""TRINITY entry point.

    python trinity/main.py                # paper trading (default, always safe)
    python trinity/main.py --check        # validate config + environment and exit
    python trinity/main.py --init-db      # create/migrate the database and exit
    python trinity/scripts/run_live.py    # the ONLY way to start live trading (gated + interactive)
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trinity.config.settings import (  # noqa: E402
    AppConfig,
    Settings,
    load_app_config,
    load_settings,
    secret_value,
)
from trinity.core.app import build_app, connect_venues, startup_reconcile  # noqa: E402
from trinity.core.log import get_logger, setup_logging  # noqa: E402
from trinity.core.models import (
    Mode,  # noqa: E402
    Side,  # noqa: E402
)
from trinity.database.repository import open_database  # noqa: E402
from trinity.monitoring.process_lock import AlreadyRunning, ProcessLock  # noqa: E402
from trinity.monitoring.watchdog import Watchdog  # noqa: E402

log = get_logger("trinity.main")


def environment_report(settings: Settings, config: AppConfig) -> list[str]:
    lines = [
        f"PAPER_TRADING={settings.paper_trading} LIVE_TRADING={settings.live_trading}",
        f"database: {settings.database_path}",
        f"crypto exchange: {settings.crypto_exchange_id} (keys: {'yes' if settings.has_crypto_credentials() else 'NO'})",
        f"stock broker: {settings.stock_broker} (keys: {'yes' if settings.has_alpaca_credentials() else 'NO'}, "
        f"alpaca paper endpoint: {settings.alpaca_paper_account})",
        f"stock data provider: {settings.stock_data_provider}",
        f"arbitrage exchanges (public data): {', '.join(settings.arb_exchanges)}",
        f"telegram: {'configured' if settings.telegram_bot_token else 'NOT configured'}; "
        f"allowed ids: {len(settings.allowed_telegram_ids)}",
        f"capital: total ${config.capital.total_capital_usd}, allocations {config.capital.allocations}, "
        f"reserve ${config.capital.reserve_usd}",
        f"risk: per trade {config.risk.max_risk_per_trade_pct:.2%}, open risk {config.risk.max_total_open_risk_pct:.0%}, "
        f"daily {config.risk.max_daily_loss_pct:.0%}, weekly {config.risk.max_weekly_loss_pct:.0%}, DD tiers "
        f"{config.risk.drawdown_warning_pct:.0%}/{config.risk.drawdown_reduce_pct:.0%}/"
        f"{config.risk.drawdown_live_lock_pct:.0%}/{config.risk.drawdown_emergency_pct:.0%}",
        f"leverage: {'enabled in config (still gated)' if config.leverage.enabled else 'OFF'}; "
        f"LIVE_LEVERAGE_ENABLED={settings.live_leverage_enabled}; reinvest profits {config.capital.reinvest_profits}",
    ]
    if settings.account_currency == "EUR" and not settings.eur_usd_rate:
        lines.append("WARNING: ACCOUNT_CURRENCY=EUR but EUR_USD_RATE not set")
    return lines


async def run(settings: Settings, config: AppConfig, mode: Mode, max_runtime: float | None = None) -> int:
    repo, notes = open_database(settings.database_path, settings.backup_dir)
    for n in notes:
        log.critical(n)
    extra = []
    telegram = None
    if settings.telegram_bot_token and settings.allowed_telegram_ids:
        from trinity.notifications.telegram_bot import TelegramControlCenter

        telegram = TelegramControlCenter(secret_value(settings.telegram_bot_token), settings.allowed_telegram_ids)
        extra.append(telegram)
    elif settings.telegram_bot_token:
        log.warning("TELEGRAM_BOT_TOKEN set but TELEGRAM_ALLOWED_USER_IDS empty: Telegram disabled for safety")
    app = build_app(settings, config, repo, mode, extra_notifiers=extra)
    for n in app.notes:
        log.warning(n)
    if notes:
        app.engine.risk.set_recovery_lock("; ".join(notes))
    if telegram is not None:
        telegram.pm = app.pm
    conn = await connect_venues(app)
    for name, res in conn.items():
        log.info("venue %s: %s", name, res)
    reports = await startup_reconcile(app)
    for r in reports:
        log.info("reconcile %s: %s %s", r.venue, "OK" if r.ok else "FAILED", r.issues)
    pm = app.pm
    await pm.start()
    watchdog = Watchdog(pm, settings.database_path.parent)
    pm.scheduler.add("watchdog", 60, watchdog.check, run_immediately=False)

    from trinity.scripts.backup import create_backup

    async def backup() -> None:
        path = await asyncio.to_thread(create_backup, repo, settings.backup_dir)
        log.info("backup written: %s", path.name)

    pm.scheduler.add("backup", 6 * 3600, backup, run_immediately=False)

    async def recheck_recovery() -> None:
        if "recovery_lock" in app.engine.risk.blocking_locks():
            await startup_reconcile(app)

    pm.scheduler.add("recovery_recheck", 300, recheck_recovery, run_immediately=False)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # Windows: KeyboardInterrupt handled below
            pass
    tasks = [asyncio.create_task(pm.scheduler.run_forever())]
    if telegram is not None:
        tasks.append(asyncio.create_task(telegram.run()))
    try:
        if max_runtime:
            await asyncio.wait_for(stop.wait(), timeout=max_runtime)
        else:
            await stop.wait()
    except (TimeoutError, KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        log.info("shutting down")
        pm.scheduler.stop()
        # Do not leave unattended entry orders: cancel unfilled BUYs. Positions and exits are kept.
        try:
            await app.engine.cancel_where(side=Side.BUY, reason="shutdown")
        except Exception:  # noqa: BLE001
            log.exception("cancel on shutdown failed")
        await pm.stop()
        if telegram is not None:
            await telegram.close()
        for t in tasks:
            t.cancel()
        await app.close()
        repo.close()
    return 0


def main(argv: list[str] | None = None, *, _live_authorized: bool = False) -> int:
    ap = argparse.ArgumentParser(description="TRINITY AI Trading System")
    ap.add_argument("--check", action="store_true", help="validate configuration and exit")
    ap.add_argument("--init-db", action="store_true", help="initialize/migrate the database and exit")
    ap.add_argument("--max-runtime", type=float, default=None, help="stop after N seconds (testing)")
    args = ap.parse_args(argv)
    settings = load_settings()
    setup_logging(settings.log_dir, settings.log_level)
    config = load_app_config()
    if args.check:
        print("\n".join(environment_report(settings, config)))
        print("configuration OK")
        return 0
    if args.init_db:
        repo, notes = open_database(settings.database_path, settings.backup_dir)
        print("database ready:", settings.database_path, "missing tables:", repo.missing_tables(), *notes)
        repo.close()
        return 0
    mode = Mode.LIVE if _live_authorized else Mode.PAPER
    if mode == Mode.PAPER and settings.live_trading:
        log.warning("LIVE_TRADING=true is ignored by main.py: live trading starts only via scripts/run_live.py")
    lock = ProcessLock(settings.lock_file)
    try:
        lock.acquire()
    except AlreadyRunning as exc:
        log.error("%s - refusing to start a second instance", exc)
        return 2
    try:
        log.info("starting TRINITY in %s mode", mode.value.upper())
        return asyncio.run(run(settings, config, mode, args.max_runtime))
    except KeyboardInterrupt:
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
