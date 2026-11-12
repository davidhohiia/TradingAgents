"""Accelerated 24-hour continuous-operation simulation of the WHOLE system.

Everything is real code (portfolio manager, bots, risk manager, execution engine, paper
exchanges, SQLite) except the market: prices come from a SYNTHETIC generator with
injected faults (random API errors, stale quotes, a 30-minute exchange outage, a crash
regime) and a simulated process restart in the middle of the run.

The resulting PnL is meaningless for real markets; the purpose is to verify stability,
recovery and risk invariants.

    python trinity/scripts/simulate_24h.py [--hours 24] [--seed 11]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trinity.config.settings import PROJECT_DIR, Settings, load_app_config  # noqa: E402
from trinity.core.app import build_app, startup_reconcile  # noqa: E402
from trinity.core.clock import SimClock  # noqa: E402
from trinity.core.log import setup_logging  # noqa: E402
from trinity.core.market_calendar import UsEquityCalendar  # noqa: E402
from trinity.core.models import AssetClass, Mode, OrderStatus, Side  # noqa: E402
from trinity.database.repository import Repository  # noqa: E402
from trinity.exchanges.simulated_market import SimulatedMarket, SymbolSpec  # noqa: E402

START = datetime(2026, 3, 2, 0, 0, tzinfo=UTC)  # Monday 00:00 UTC (US session 14:30-21:00 UTC)


def build_markets(clock: SimClock, seed: int, hours: float, cal: UsEquityCalendar):
    outage = (START + timedelta(hours=hours * 0.3), START + timedelta(hours=hours * 0.3, minutes=30))
    crypto = SimulatedMarket(
        "sim-crypto", AssetClass.CRYPTO,
        [SymbolSpec("BTC/USDT", 60000, "1h", annual_vol=0.55, annual_drift=4.0, level_qty_usd=60_000,
                    regimes=[(hours * 0.55, -25.0), (hours * 0.7, 2.0)]),
         SymbolSpec("ETH/USDT", 3000, "1h", annual_vol=0.7, annual_drift=4.0, level_qty_usd=60_000),
         SymbolSpec("SOL/USDT", 150, "1h", annual_vol=0.9, annual_drift=4.0, level_qty_usd=60_000)],
        clock, seed=seed, error_rate=0.02, stale_rate=0.01, outages=[outage])
    stocks = SimulatedMarket(
        "sim-stocks", AssetClass.STOCK,
        [SymbolSpec("SPY", 500, "1Day", annual_vol=0.18, annual_drift=0.3, bar_volume=8e7, history_bars=260),
         SymbolSpec("QQQ", 430, "1Day", annual_vol=0.22, annual_drift=0.3, bar_volume=4e7, history_bars=260),
         SymbolSpec("VTI", 250, "1Day", annual_vol=0.17, annual_drift=0.3, bar_volume=4e6, history_bars=260)],
        clock, seed=seed + 1, error_rate=0.01, session_check=cal.is_open)
    arb = {name: SimulatedMarket(name, AssetClass.CRYPTO,
                                 [SymbolSpec("BTC/USDT", 60000 * (1 + 0.0004 * i), "1h", spread_bps=3),
                                  SymbolSpec("ETH/USDT", 3000 * (1 - 0.0003 * i), "1h", spread_bps=3)],
                                 clock, seed=seed + 10, error_rate=0.03)  # same price path, small offsets
           for i, name in enumerate(["sim-ex-a", "sim-ex-b", "sim-ex-c"])}
    return crypto, stocks, arb


def check_invariants(repo: Repository, app, config) -> list[str]:  # noqa: ANN001
    problems = []
    for name, v in app.venues.items():
        for asset, b in v.balances.items():
            if b.free < -1e-6 or b.used < -1e-6:
                problems.append(f"negative balance {name} {asset}: {b}")
    for p in app.engine.positions.all():
        if p.quantity < -1e-9:
            problems.append(f"short position {p.strategy_id} {p.symbol}")
    orders = repo.list_orders(Mode.PAPER, limit=100000)
    ids = [o.client_order_id for o in orders]
    if len(ids) != len(set(ids)):
        problems.append("duplicate client order ids")
    for o in orders:
        if o.side == Side.BUY and o.price and o.quantity * o.price > config.risk.max_order_notional_usd + 1e-6:
            problems.append(f"order above max notional {o.client_order_id}")
        if o.filled_quantity > o.quantity + 1e-9:
            problems.append(f"overfill {o.client_order_id}")
    orphan = repo.query("SELECT COUNT(*) FROM executions e LEFT JOIN orders o ON o.client_order_id=e.client_order_id "
                        "WHERE o.client_order_id IS NULL")[0][0]
    if orphan:
        problems.append(f"{orphan} executions without orders")
    caps = app.pm.balance_data()["strategies"]
    for sid, c in caps.items():
        if c["available"] < -1e-9:
            problems.append(f"negative available capital {sid}")
        if sid == "arbitrage" and (c["deployed"] or c["reserved"]):
            problems.append("arbitrage used real (paper) capital")
    eq = app.engine.account_equity()
    if app.engine.open_risk() > config.risk.max_total_open_risk_pct * eq * 1.25:
        problems.append(f"open risk {app.engine.open_risk():.2f} far above limit")
    if repo.query("SELECT COUNT(*) FROM orders WHERE leverage > 1")[0][0]:
        problems.append("leveraged order placed while leverage is disabled")
    for o in orders:
        if o.side == Side.BUY and o.stop_price and o.price and \
                o.quantity * (o.price - o.stop_price) > config.risk.max_risk_per_trade_pct * eq * 1.05 + 0.01:
            problems.append(f"order risk above 0.75% of equity {o.client_order_id}")
    exp = app.pm.risk_data()["exposure"]
    if exp > config.risk.max_total_exposure_pct * app.engine.account_equity() + 1:
        problems.append(f"exposure {exp:.2f} above limit")
    return problems


async def simulate(hours: float, seed: int, tick: float, out_dir: Path) -> dict:
    config = load_app_config()
    tmp = Path(tempfile.mkdtemp(prefix="trinity-sim-"))
    settings = Settings(_env_file=None, database_path=tmp / "sim.db", backup_dir=tmp / "bk",  # type: ignore[call-arg]
                        log_dir=tmp / "logs", lock_file=tmp / "sim.lock")
    clock = SimClock(START)
    cal = UsEquityCalendar(config.session)
    crypto, stocks, arb = build_markets(clock, seed, hours, cal)
    repo = Repository(settings.database_path)
    repo.migrate()
    wall = time.time()
    invariant_violations: list[str] = []
    ticks = 0
    restart_at = START + timedelta(hours=hours / 2)
    restarted = False
    deposit_at = START + timedelta(hours=hours * 0.75)
    deposited = False
    app = None

    async def boot():
        a = build_app(settings, config, repo, Mode.PAPER, clock=clock, crypto_data=crypto, stock_data=stocks,
                      arb_sources=arb)
        reps = await startup_reconcile(a)
        await a.pm.start()
        return a, reps

    app, _ = await boot()
    end = START + timedelta(hours=hours)
    reconcile_after_restart = None
    while clock.now() < end:
        await app.pm.tick()
        ticks += 1
        if ticks % 360 == 0:
            invariant_violations += check_invariants(repo, app, config)
        if not deposited and clock.now() >= deposit_at:
            deposited = True
            before = app.pm.equity_breakdown()
            app.pm.register_deposit(200, "simulated monthly deposit")
            after = app.pm.equity_breakdown()
            if abs(after["trading_profit"] - before["trading_profit"]) > 1e-6:
                invariant_violations.append("deposit changed trading profit")
        if not restarted and clock.now() >= restart_at:
            # simulated crash/restart: drop the whole object graph, keep DB + exchange state
            restarted = True
            await app.pm.stop()
            app, reps = await boot()
            reconcile_after_restart = [{"venue": r.venue, "ok": r.ok, "issues": r.issues} for r in reps]
        clock.advance(tick)
    invariant_violations += check_invariants(repo, app, config)
    eq = app.pm.equity_breakdown()
    orders = repo.list_orders(Mode.PAPER, limit=100000)
    status_counts = Counter(o.status.value for o in orders)
    risk_events = Counter(r["event_type"] for r in repo.query("SELECT event_type FROM risk_events"))
    stats = repo.trade_stats(Mode.PAPER)
    per_strategy = {sid: dict(repo.trade_stats(Mode.PAPER, sid)) for sid in ("crypto_momentum", "stock_trend")}
    arb_rows = repo.query("SELECT COUNT(*), SUM(accepted), SUM(virtual_executed) FROM arbitrage_opportunities")[0]
    report = {
        "NOTE": "SYNTHETIC market data with injected faults. PnL is NOT evidence of real performance.",
        "simulated_hours": hours, "tick_seconds": tick, "ticks": ticks, "wall_seconds": round(time.time() - wall, 1),
        "restart_performed": restarted, "reconcile_after_restart": reconcile_after_restart,
        "orders_total": len(orders), "orders_by_status": dict(status_counts),
        "open_orders_end": sum(1 for o in orders if o.status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED)),
        "executions": repo.query("SELECT COUNT(*) FROM executions")[0][0],
        "closed_trades": int(stats["n"]), "virtual_net_pnl_usd": round(float(stats["pnl"]), 4),
        "fees_usd": round(float(stats["fees"]), 4), "per_strategy": per_strategy,
        "equity_end_virtual": round(eq["equity"], 4), "unrealized_end": round(eq["unrealized"], 4),
        "risk_events": dict(risk_events), "locks_end": app.engine.risk.blocking_locks(),
        "bot_restarts": app.pm.restarts, "bot_status_end": {sid: b.status().value for sid, b in app.pm.bots.items()},
        "data_source_calls": {"crypto": crypto.calls, "crypto_injected_errors": crypto.errors,
                              "stocks": stocks.calls, "stocks_injected_errors": stocks.errors},
        "arbitrage": {"recorded": arb_rows[0], "accepted": arb_rows[1] or 0, "virtual_executed": arb_rows[2] or 0},
        "scheduler": {n: {"runs": t.runs, "failures": t.failures} for n, t in app.pm.scheduler.tasks.items()},
        "invariant_violations": sorted(set(invariant_violations)),
        "deposit_registered": deposited, "capital": {k: v for k, v in app.pm.capital_data().items()
                                                     if k in ("contributed", "equity", "trading_profit", "reserve",
                                                              "allocations")},
        "per_sub_strategy": app.pm.performance.per_sub_strategy(Mode.PAPER),
        "database": str(settings.database_path),
    }
    await app.pm.stop()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "sim_24h_report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--tick", type=float, default=10)
    ap.add_argument("--out", type=Path, default=PROJECT_DIR / "data")
    args = ap.parse_args()
    setup_logging(None, "ERROR")
    report = asyncio.run(simulate(args.hours, args.seed, args.tick, args.out))
    print(json.dumps(report, indent=2, default=str))
    return 1 if report["invariant_violations"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
