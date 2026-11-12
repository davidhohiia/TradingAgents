"""Risk manager, position sizing, daily loss, drawdown, circuit breaker, capital allocation."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from trinity.config.settings import CapitalConfig, RiskConfig
from trinity.core.capital_allocator import CapitalAllocator
from trinity.core.models import (
    AssetClass,
    Mode,
    OrderIntent,
    OrderRecord,
    OrderStatus,
    OrderType,
    Position,
    Side,
)
from trinity.risk.circuit_breaker import BreakerState, CircuitBreaker
from trinity.risk.exposure_manager import ExposureManager, ExposureSnapshot
from trinity.risk.position_sizing import size_by_stop, size_fixed_notional
from trinity.risk.risk_manager import DRAWDOWN_CONFIRM_PHRASE, RiskContext


def intent(system, **kw) -> OrderIntent:
    base = dict(strategy_id="crypto_grid", venue="crypto", symbol="BTC/USDT", side=Side.BUY,
                order_type=OrderType.LIMIT, quantity=0.0003, reference_price=60000.0, price=59900.0,
                stop_price=57000.0, market_data_ts=system.clock.now(), asset_class=AssetClass.CRYPTO)
    base.update(kw)
    return OrderIntent(**base)


def ctx(system, exposure: ExposureSnapshot | None = None, capital: float = 150, available: float = 150,
        **kw) -> RiskContext:
    m = system.config.instruments["BTC/USDT"].to_market_info("BTC/USDT")
    return RiskContext(exposure or ExposureSnapshot(), m, capital, available, system.clock.now(), **kw)


# ---------------------------------------------------------------- position sizing
def test_size_by_stop_uses_stop_distance():
    r = size_by_stop(strategy_capital=150, risk_pct=0.01, entry=100, stop=95, max_order_notional=1000,
                     max_position_notional=1000, available_cash=1000, qty_step=0.0001, min_qty=0.0001, min_notional=1)
    assert r.ok and r.limited_by == "risk"
    assert r.quantity == pytest.approx(0.3, abs=1e-4)  # 1.5 USD / 5 USD per unit
    assert r.risk_usd <= 1.5 + 1e-9


def test_size_capped_by_max_order_and_cash():
    r = size_by_stop(strategy_capital=150, risk_pct=0.01, entry=100, stop=99.9, max_order_notional=75,
                     max_position_notional=1000, available_cash=1000, qty_step=0.01, min_qty=0.01, min_notional=1)
    assert r.limited_by == "max_order_notional" and r.notional <= 75
    r2 = size_by_stop(strategy_capital=150, risk_pct=0.01, entry=100, stop=99.9, max_order_notional=75,
                      max_position_notional=1000, available_cash=20, qty_step=0.01, min_qty=0.01, min_notional=1)
    assert r2.limited_by == "available_cash" and r2.notional <= 20


def test_size_rejects_invalid_or_too_small():
    assert not size_by_stop(strategy_capital=150, risk_pct=0.01, entry=100, stop=101, max_order_notional=75,
                            max_position_notional=75, available_cash=75, qty_step=1, min_qty=1, min_notional=1).ok
    # whole shares only and price too high -> zero
    r = size_by_stop(strategy_capital=150, risk_pct=0.01, entry=600, stop=580, max_order_notional=75,
                     max_position_notional=90, available_cash=150, qty_step=1, min_qty=1, min_notional=1)
    assert not r.ok


def test_no_martingale_size_never_grows_after_loss():
    before = size_by_stop(strategy_capital=150, risk_pct=0.01, entry=100, stop=95, max_order_notional=1000,
                          max_position_notional=1000, available_cash=1000, qty_step=1e-4, min_qty=1e-4, min_notional=1)
    after_loss = size_by_stop(strategy_capital=140, risk_pct=0.01, entry=100, stop=95, max_order_notional=1000,
                              max_position_notional=1000, available_cash=1000, qty_step=1e-4, min_qty=1e-4,
                              min_notional=1)
    assert after_loss.quantity < before.quantity
    f1 = size_fixed_notional(notional=20, price=100, qty_step=1e-4, min_qty=1e-4, min_notional=5)
    assert f1.quantity == pytest.approx(0.2)


# ---------------------------------------------------------------- config validation
def test_config_forbids_leverage_margin_short():
    with pytest.raises(ValidationError):
        RiskConfig(leverage=2)
    with pytest.raises(ValidationError):
        RiskConfig(allow_margin=True)
    with pytest.raises(ValidationError):
        RiskConfig(allow_short=True)
    with pytest.raises(ValidationError):
        RiskConfig(max_risk_per_trade_pct=0.05)


def test_capital_config_reserve_and_sum(config):
    assert config.capital.reserve_usd == 200
    assert config.allocation("crypto_grid") == 150 and config.allocation("arbitrage") == 0
    with pytest.raises(ValidationError):
        CapitalConfig(total_capital_usd=500, allocations={"a": 400}, reserve_usd=200)
    with pytest.raises(ValidationError):
        CapitalConfig(total_capital_usd=500, allocations={"a": 100}, reserve_usd=200, allow_profit_reinvestment=True)


# ---------------------------------------------------------------- order checks
def test_valid_order_passes(system):
    d = system.risk.check_order(intent(system), ctx(system))
    assert d.approved, d.reasons


def test_rejects_without_stop(system):
    d = system.risk.check_order(intent(system, stop_price=None), ctx(system))
    assert not d.approved and any("stop" in r for r in d.reasons)


def test_rejects_excess_risk_per_trade(system):
    # qty*(price-stop) = 0.001 * 9900 = 9.9 > 1.5
    d = system.risk.check_order(intent(system, quantity=0.001, stop_price=50000), ctx(system))
    assert not d.approved and any("trade risk" in r for r in d.reasons)


def test_rejects_max_order_notional(system):
    d = system.risk.check_order(intent(system, quantity=0.002, stop_price=59800), ctx(system))
    assert not d.approved and any("max" in r for r in d.reasons)


def test_rejects_stale_data(system):
    old = system.clock.now() - timedelta(seconds=120)
    d = system.risk.check_order(intent(system, market_data_ts=old), ctx(system))
    assert not d.approved and any("stale" in r for r in d.reasons)


def test_rejects_total_exposure(system):
    snap = ExposureSnapshot()
    snap.total = 290.0
    d = system.risk.check_order(intent(system), ctx(system, snap))
    assert not d.approved and any("total exposure" in r for r in d.reasons)


def test_rejects_correlated_exposure(system):
    snap = ExposureSnapshot()
    snap.by_group["crypto_major"] = 140.0
    d = system.risk.check_order(intent(system), ctx(system, snap))
    assert not d.approved and any("correlated" in r for r in d.reasons)


def test_rejects_max_open_positions(system):
    snap = ExposureSnapshot()
    snap.open_positions = {(f"s{i}", f"X{i}") for i in range(6)}
    d = system.risk.check_order(intent(system), ctx(system, snap))
    assert not d.approved and any("open positions" in r for r in d.reasons)


def test_rejects_zero_capital_strategy(system):
    d = system.risk.check_order(intent(system, strategy_id="arbitrage"), ctx(system, capital=0, available=0))
    assert not d.approved


def test_rejects_short_sell(system):
    d = system.risk.check_order(intent(system, side=Side.SELL, price=60100, stop_price=None), ctx(system))
    assert not d.approved and any("short" in r for r in d.reasons)


def test_rejects_fat_finger_limit(system):
    d = system.risk.check_order(intent(system, price=64000), ctx(system))
    assert not d.approved and any("above market" in r for r in d.reasons)


def test_rejects_when_breaker_open(system):
    b = system.breakers.get("crypto")
    for _ in range(system.config.risk.api_failure_threshold):
        b.record_failure("timeout")
    d = system.risk.check_order(intent(system), ctx(system))
    assert not d.approved and any("circuit breaker" in r for r in d.reasons)


def test_order_rate_limit(system):
    for _ in range(system.config.risk.max_orders_per_minute):
        system.risk.record_submission(system.clock.now())
    d = system.risk.check_order(intent(system), ctx(system))
    assert not d.approved and any("rate" in r for r in d.reasons)


# ---------------------------------------------------------------- daily loss / drawdown
def test_daily_loss_limit_locks_entries_until_next_day(system):
    rm = system.risk
    assert rm.on_equity(500) == []
    actions = rm.on_equity(489.9)  # -10.1 USD <= -2% of 500
    assert [a.kind for a in actions] == ["DAILY_LOCK"]
    assert "daily_loss_lock" in rm.blocking_locks()
    assert not rm.check_order(intent(system), ctx(system)).approved
    # reducing orders still allowed
    snap = ExposureSnapshot()
    snap.position_qty[("crypto_grid", "BTC/USDT")] = 0.001
    sell = intent(system, side=Side.SELL, quantity=0.0005, price=60000, stop_price=None)
    assert rm.check_order(sell, ctx(system, snap)).approved
    system.clock.advance(86400)
    rm.on_equity(489.9)
    assert "daily_loss_lock" not in rm.blocking_locks()


def test_daily_pnl_includes_unrealized_via_equity(system):
    rm = system.risk
    rm.on_equity(500)
    st = rm.drawdown.update(495, system.clock.now())
    assert st.daily_pnl == pytest.approx(-5)


def test_drawdown_lock_requires_manual_confirmation(system):
    rm = system.risk
    rm.on_equity(520)
    system.clock.advance(86400)
    acts = rm.on_equity(478)  # 8.08% below peak 520
    assert "DRAWDOWN_LOCK" in [a.kind for a in acts]
    assert "drawdown_lock" in rm.blocking_locks()
    ok, remaining = rm.resume()  # /resume must NOT clear it
    assert not ok and "drawdown_lock" in remaining
    assert not rm.confirm_drawdown_review("yes", 478)
    assert rm.confirm_drawdown_review(DRAWDOWN_CONFIRM_PHRASE, 478)
    assert "drawdown_lock" not in rm.blocking_locks()
    assert rm.state.live_disabled  # live stays disabled until run_live re-confirmation


def test_drawdown_warning_before_lock(system):
    rm = system.risk
    rm.on_equity(500)
    acts = rm.on_equity(474)  # 5.2%
    assert [a.kind for a in acts] == ["DAILY_LOCK", "DRAWDOWN_WARNING"]


def test_drawdown_peak_survives_restart(system, tmp_path, config):
    from trinity.risk.risk_manager import RiskManager

    system.risk.on_equity(530)
    rm2 = RiskManager(config, system.repo, Mode.PAPER, system.breakers, system.clock)
    st = rm2.drawdown.update(500, system.clock.now())
    assert st.peak_equity == 530 and st.drawdown_pct == pytest.approx(30 / 530)


def test_emergency_stop_blocks_everything_including_exits(system):
    rm = system.risk
    rm.emergency_stop("test")
    snap = ExposureSnapshot()
    snap.position_qty[("crypto_grid", "BTC/USDT")] = 0.001
    sell = intent(system, side=Side.SELL, quantity=0.0005, price=60000, stop_price=None, protective_exit=True)
    assert not rm.check_order(sell, ctx(system, snap)).approved
    assert not rm.resume()[0]
    assert not rm.clear_emergency_stop("please")
    assert rm.clear_emergency_stop("CLEAR EMERGENCY STOP")


def test_pause_allows_only_reducing(system):
    rm = system.risk
    rm.pause()
    assert not rm.check_order(intent(system), ctx(system)).approved
    snap = ExposureSnapshot()
    snap.position_qty[("crypto_grid", "BTC/USDT")] = 0.001
    sell = intent(system, side=Side.SELL, quantity=0.0005, price=60000, stop_price=None)
    assert rm.check_order(sell, ctx(system, snap)).approved
    assert rm.resume() == (True, [])


# ---------------------------------------------------------------- circuit breaker
def test_circuit_breaker_open_halfopen_close(system):
    b = CircuitBreaker("x", 3, 60, system.clock)
    assert b.allow()
    b.record_failure("e1")
    b.record_failure("e2")
    assert b.allow()
    assert b.record_failure("e3")
    assert b.state == BreakerState.OPEN and not b.allow()
    system.clock.advance(61)
    assert b.state == BreakerState.HALF_OPEN and b.allow()
    b.record_failure("probe failed")
    assert b.state == BreakerState.OPEN
    system.clock.advance(61)
    b.record_success()
    assert b.state == BreakerState.CLOSED


# ---------------------------------------------------------------- capital allocation
def _pos(sid, qty, px):
    return Position(sid, "crypto", "BTC/USDT", Mode.PAPER, quantity=qty, avg_entry_price=px)


def _order(sid, cid, qty, px, side=Side.BUY):
    return OrderRecord(cid, sid, "crypto", "BTC/USDT", Mode.PAPER, side, OrderType.LIMIT, qty, OrderStatus.OPEN,
                       price=px)


def test_allocator_budget_reserved_deployed(config):
    a = CapitalAllocator(config)
    c = a.compute("crypto_grid", [_pos("crypto_grid", 0.001, 60000)], [_order("crypto_grid", "c1", 0.0005, 60000)], 0)
    assert c.deployed == pytest.approx(60) and c.reserved == pytest.approx(30)
    assert c.available == pytest.approx(60)


def test_allocator_losses_reduce_profits_do_not_increase(config):
    a = CapitalAllocator(config)
    assert a.compute("crypto_grid", [], [], -20).effective_capital == pytest.approx(130)
    assert a.compute("crypto_grid", [], [], +50).effective_capital == pytest.approx(150)


def test_allocator_never_negative_and_no_double_reservation(config):
    a = CapitalAllocator(config)
    c = a.compute("crypto_grid", [_pos("crypto_grid", 0.003, 60000)], [], 0)  # 180 deployed > 150
    assert c.available == 0.0
    assert a.reserve_inflight("crypto_grid", "x1", 50, 100)
    assert not a.reserve_inflight("crypto_grid", "x1", 50, 100)  # same order twice
    assert not a.reserve_inflight("crypto_grid", "x2", 60, 100)  # would exceed available
    a.release_inflight("x1")
    assert a.reserve_inflight("crypto_grid", "x2", 60, 100)


def test_allocator_reserve_and_other_strategies_isolated(config):
    a = CapitalAllocator(config)
    assert a.compute("unknown", [], [], 0).available == 0
    c = a.compute("stock_trend", [_pos("crypto_grid", 0.002, 60000)], [], 0)
    assert c.available == 150  # crypto usage does not consume stock budget


def test_exposure_manager_counts_pending_buys(config):
    em = ExposureManager(config.correlation_group)
    snap = em.snapshot([_pos("crypto_grid", 0.001, 60000)],
                       [_order("crypto_grid", "a", 0.0005, 60000), _order("crypto_grid", "b", 0.0002, 61000, Side.SELL)])
    assert snap.total == pytest.approx(90)
    assert snap.by_group["crypto_major"] == pytest.approx(90)
    assert snap.pending_sell_qty[("crypto_grid", "BTC/USDT")] == pytest.approx(0.0002)
