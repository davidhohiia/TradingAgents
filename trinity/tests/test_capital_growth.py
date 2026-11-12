"""Capital Growth Manager: deposits, contributed vs profit, reserve, reallocation, Modified Dietz."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trinity.core.capital_growth import APPROVAL_PHRASE, CapitalGrowthManager
from trinity.core.models import AssetClass, Mode, OrderIntent, OrderType, Side
from trinity.tests.test_portfolio import make_pm


def growth(system, config) -> CapitalGrowthManager:
    return CapitalGrowthManager(system.repo, config, system.clock)


def test_initial_state_records_initial_capital(system, config):
    g = growth(system, config)
    assert g.contributed() == 500
    assert g.reserve() == 250
    assert g.allocations() == {"crypto_momentum": 150, "stock_trend": 100, "arbitrage": 0}
    assert [r["kind"] for r in system.repo.list_deposits()] == ["initial"]
    g2 = growth(system, config)  # idempotent re-initialisation after restart
    assert g2.contributed() == 500 and len(system.repo.list_deposits()) == 1


def test_deposit_goes_to_reserve_and_is_not_profit(system, config):
    pm, _ = make_pm(system, config)
    pm.risk.on_equity(pm.equity_breakdown()["equity"])
    before = pm.equity_breakdown()
    pm.register_deposit(200, "monthly")
    after = pm.equity_breakdown()
    assert pm.growth.reserve() == 450 and pm.growth.allocations()["crypto_momentum"] == 150  # not auto-allocated
    assert after["equity"] == pytest.approx(before["equity"] + 200)
    assert after["trading_profit"] == pytest.approx(before["trading_profit"])  # deposit is not income
    # baselines shifted: a deposit can neither hide a drawdown nor trigger "profit"
    st = pm.risk.on_equity(after["equity"])
    assert st == [] and pm.risk.last_status.daily_pnl == pytest.approx(0)
    assert pm.risk.last_status.peak_equity == pytest.approx(700)


def test_eur_deposit_converted_with_fx(system, config):
    g = growth(system, config)
    g.register_deposit(100, currency="EUR", fx_rate=1.08)
    row = system.repo.list_deposits()[-1]
    assert row["amount_usd"] == pytest.approx(108) and row["currency"] == "EUR" and row["amount_original"] == 100
    with pytest.raises(ValueError):
        g.register_deposit(-5)


def test_deposit_does_not_hide_drawdown(system, config):
    pm, _ = make_pm(system, config)
    pm.risk.on_equity(500)
    p = system.positions.get("stock_trend", "stocks", "SPY")
    p.realized_pnl -= 60  # 12% loss
    system.clock.advance(8 * 86400)
    pm.register_deposit(200)
    pm.risk.on_equity(pm.equity_breakdown()["equity"])  # 640 vs adjusted peak 700
    assert pm.risk.last_status.drawdown_pct == pytest.approx(60 / 700)


def _seed_trades(system, sid: str, n: int, pnl: float):
    for i in range(n):
        system.repo.upsert_trade_fill(strategy_id=sid, venue="crypto", symbol="BTC/USDT", mode=Mode.PAPER,
                                      quantity=0.001, entry_price=1, exit_price=1, entry_time=system.clock.now(),
                                      exit_time=system.clock.now(), fees_usd=0.01, realized_pnl=pnl if i % 3 else -pnl / 2,
                                      exit_reason="", exit_client_order_id=f"{sid}{i}", is_virtual=True)


def test_reallocation_requires_evidence_and_manual_approval(system, config):
    pm, _ = make_pm(system, config)
    pm.risk.on_equity(500)
    ids, notes = pm.propose_reallocation()
    assert ids == [] and any("only 0" in n for n in notes)  # no evidence -> no proposal
    _seed_trades(system, "crypto_momentum", 40, 1.0)
    pm.register_deposit(200)
    ids, notes = pm.propose_reallocation()
    assert len(ids) == 1
    prop = pm.growth.pending()[0]
    assert prop["strategy_id"] == "crypto_momentum" and prop["status"] == "pending"
    assert pm.growth.allocations()["crypto_momentum"] == 150  # nothing applied automatically
    ok, why = pm.approve_reallocation(ids[0], "yes please")
    assert not ok and "confirmation" in why[0]
    usdt_before = system.crypto.balances["USDT"].free
    ok, why = pm.approve_reallocation(ids[0], APPROVAL_PHRASE)
    assert ok, why
    assert pm.growth.allocations()["crypto_momentum"] == pytest.approx(150 + prop["amount_usd"])
    assert pm.growth.reserve() == pytest.approx(450 - prop["amount_usd"])
    assert system.crypto.balances["USDT"].free == pytest.approx(usdt_before + prop["amount_usd"])  # paper transfer
    assert pm.growth.allocation("crypto_momentum") <= 0.45 * pm.growth.contributed() + 1e-9
    ok, why = pm.approve_reallocation(ids[0], APPROVAL_PHRASE)  # no double application
    assert not ok


def test_no_reallocation_under_risk_locks_or_drawdown(system, config):
    pm, _ = make_pm(system, config)
    _seed_trades(system, "crypto_momentum", 40, 1.0)
    pm.risk.pause("test")
    ids, notes = pm.propose_reallocation()
    assert ids == [] and "risk locks" in notes[0]
    pm.risk.resume()
    pm.risk.on_equity(500)
    system.clock.advance(8 * 86400)
    pm.risk.on_equity(445)  # 11% drawdown
    ids, notes = pm.propose_reallocation()
    assert ids == [] and "drawdown" in notes[0]


def test_arbitrage_never_receives_capital(system, config):
    g = growth(system, config)
    ids, notes = g.propose({"arbitrage": {"trades": 500, "net_pnl": 100, "profit_factor": 3}}, [], 0.0, set())
    assert ids == [] and "scan-only" in notes[0]


def test_modified_dietz_excludes_deposits(system, config):
    g = growth(system, config)
    t0 = datetime(2026, 3, 1, tzinfo=UTC)
    g.register_deposit(200, ts=t0 + timedelta(days=15))
    pts = [(t0, 500.0), (t0 + timedelta(days=14), 510.0), (t0 + timedelta(days=30), 720.0)]
    m = g.monthly_returns(pts)[0]
    # profit = 720 - 500 - 200 = 20 ; denominator = 500 + 200 * (31-15)/31
    assert m["profit"] == pytest.approx(20)
    assert m["return"] == pytest.approx(20 / (500 + 200 * 16 / 31))
    assert m["target_informational"] == 0.30


async def test_reinvested_profit_increases_strategy_capital(system, config):
    pm, _ = make_pm(system, config)
    pos = system.positions.get("crypto_momentum", "crypto", "BTC/USDT")
    pos.realized_pnl = 30.0
    cap = pm.allocator.compute("crypto_momentum", system.positions.all(), [], 30.0)
    assert cap.effective_capital == pytest.approx(180)
    b = await system.crypto_market.fetch_order_book("BTC/USDT")
    res = await system.engine.submit(OrderIntent(
        strategy_id="crypto_momentum", venue="crypto", symbol="BTC/USDT", side=Side.BUY, order_type=OrderType.MARKET,
        quantity=0.0003, reference_price=b.best_ask, stop_price=b.best_ask * 0.98, market_data_ts=b.ts,
        asset_class=AssetClass.CRYPTO))
    assert res.accepted
