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
    base = dict(strategy_id="crypto_momentum", venue="crypto", symbol="BTC/USDT", side=Side.BUY,
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
def test_config_validation_of_risk_and_leverage_limits(config):
    from trinity.config.settings import ABSOLUTE_MAX_LEVERAGE, LeverageConfig

    with pytest.raises(ValidationError):
        RiskConfig(max_risk_per_trade_pct=0.05)
    with pytest.raises(ValidationError):  # tiers must be increasing
        RiskConfig(drawdown_warning_pct=0.15, drawdown_reduce_pct=0.12)
    with pytest.raises(ValidationError):  # absolute limit can never exceed 20%
        RiskConfig(drawdown_emergency_pct=0.25)
    with pytest.raises(ValidationError):
        LeverageConfig(max_leverage=ABSOLUTE_MAX_LEVERAGE + 0.5)
    with pytest.raises(ValidationError):
        LeverageConfig(symbols=["SOL/USDT"])
    r = config.risk
    assert (r.max_risk_per_trade_pct, r.max_total_open_risk_pct, r.max_daily_loss_pct, r.max_weekly_loss_pct) == \
        (0.0075, 0.03, 0.03, 0.06)
    assert (r.drawdown_warning_pct, r.drawdown_reduce_pct, r.drawdown_live_lock_pct, r.drawdown_emergency_pct) == \
        (0.10, 0.12, 0.15, 0.20)
    assert config.leverage.enabled is False and config.leverage.max_leverage == 1.5


def test_capital_config_reserve_and_sum(config):
    assert config.capital.reserve_usd == 250 and config.capital.reinvest_profits
    assert config.allocation("crypto_momentum") == 150 and config.allocation("stock_trend") == 100
    assert config.allocation("arbitrage") == 0
    with pytest.raises(ValidationError):
        CapitalConfig(total_capital_usd=500, allocations={"a": 400}, reserve_usd=200)


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
    snap.by_group["crypto_major"] = 160.0  # + 18 > 35% of 500
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
    actions = rm.on_equity(484.9)  # -15.1 USD <= -3% of 500
    assert [a.kind for a in actions] == ["DAILY_LOCK"]
    assert "daily_loss_lock" in rm.blocking_locks()
    assert not rm.check_order(intent(system), ctx(system)).approved
    # reducing orders still allowed
    snap = ExposureSnapshot()
    snap.position_qty[("crypto_momentum", "BTC/USDT")] = 0.001
    sell = intent(system, side=Side.SELL, quantity=0.0005, price=60000, stop_price=None)
    assert rm.check_order(sell, ctx(system, snap)).approved
    system.clock.advance(86400)
    rm.on_equity(484.9)
    assert "daily_loss_lock" not in rm.blocking_locks()


def test_daily_pnl_includes_unrealized_via_equity(system):
    rm = system.risk
    rm.on_equity(500)
    st = rm.drawdown.update(495, system.clock.now())
    assert st.daily_pnl == pytest.approx(-5)


def _walk(rm, system, equities):
    acts = []
    for e in equities:
        system.clock.advance(8 * 86400)  # new day and new week: isolate drawdown tiers from daily/weekly limits
        acts += [x.kind for x in rm.on_equity(e)]
    return acts


def test_drawdown_tiers_warning_reduce_lock_hardstop(system):
    rm = system.risk
    rm.on_equity(1000)
    assert _walk(rm, system, [895]) == ["DRAWDOWN_WARNING"]  # 10.5%
    assert rm.activity_multiplier() == 1.0
    assert _walk(rm, system, [875]) == ["REDUCED_ACTIVITY"]  # 12.5%
    assert rm.activity_multiplier() == 0.5
    assert rm.max_trade_risk_usd(875) == pytest.approx(875 * 0.0075 * 0.5)
    assert _walk(rm, system, [845]) == ["DRAWDOWN_LOCK"]  # 15.5%
    assert "drawdown_lock" in rm.blocking_locks() and rm.state.live_disabled
    assert _walk(rm, system, [795]) == ["HARD_STOP"]  # 20.5%
    assert "hard_stop" in rm.blocking_locks()


def test_reduced_activity_halves_allowed_trade_risk(system):
    rm = system.risk
    rm.on_equity(500)
    _walk(rm, system, [438])  # 12.4% drawdown
    # 0.0006 * (60000-55000) = 3.0 > 0.75% x 0.5 x 438 = 1.64
    d = rm.check_order(intent(system, quantity=0.0006, price=60000, stop_price=55000), ctx(system, equity=438))
    assert not d.approved and any("x0.5" in r for r in d.reasons)


def test_drawdown_lock_requires_manual_confirmation(system):
    rm = system.risk
    rm.on_equity(520)
    _walk(rm, system, [440])  # 15.4% below peak 520
    assert "drawdown_lock" in rm.blocking_locks()
    ok, remaining = rm.resume()  # /resume must NOT clear it
    assert not ok and "drawdown_lock" in remaining
    snap = ExposureSnapshot()
    snap.position_qty[("crypto_momentum", "BTC/USDT")] = 0.001
    plain = intent(system, side=Side.SELL, quantity=0.0005, price=60000, stop_price=None)
    prot = intent(system, side=Side.SELL, quantity=0.0005, price=60000, stop_price=None, protective_exit=True)
    assert not rm.check_order(plain, ctx(system, snap)).approved
    assert rm.check_order(prot, ctx(system, snap)).approved
    assert not rm.confirm_drawdown_review("yes", 440)
    assert rm.confirm_drawdown_review(DRAWDOWN_CONFIRM_PHRASE, 440)
    assert "drawdown_lock" not in rm.blocking_locks()
    assert rm.state.live_disabled  # live stays disabled until run_live re-confirmation


def test_hard_stop_requires_separate_manual_clear(system):
    from trinity.risk.risk_manager import HARD_STOP_CLEAR_PHRASE

    rm = system.risk
    rm.on_equity(500)
    _walk(rm, system, [395])
    assert "hard_stop" in rm.blocking_locks()
    assert not rm.resume()[0]
    rm.confirm_drawdown_review(DRAWDOWN_CONFIRM_PHRASE, 395)
    assert "hard_stop" in rm.blocking_locks()  # the 15% confirmation does not clear the 20% hard stop
    assert not rm.clear_hard_stop("whatever", 395)
    assert rm.clear_hard_stop(HARD_STOP_CLEAR_PHRASE, 395)
    assert not {"hard_stop", "drawdown_lock"} & set(rm.blocking_locks())


def test_weekly_loss_limit(system):
    rm = system.risk
    rm.on_equity(500)  # Monday
    for e in (490, 480, 471):  # about -2% per day: never a daily lock, -5.8% for the week so far
        system.clock.advance(86400)
        rm.on_equity(e)
    assert "weekly_loss_lock" not in rm.blocking_locks()
    system.clock.advance(86400)
    acts = [a.kind for a in rm.on_equity(469)]  # -6.2% for the week
    assert "WEEKLY_LOCK" in acts and "weekly_loss_lock" in rm.blocking_locks()
    assert not rm.check_order(intent(system), ctx(system)).approved
    system.clock.advance(7 * 86400)
    rm.on_equity(469)
    assert "weekly_loss_lock" not in rm.blocking_locks()


def test_total_open_risk_limit(system):
    # existing open risk 13 USD + new trade 0.87 > 3% of 450 = 13.5
    d = system.risk.check_order(intent(system), ctx(system, equity=450, open_risk=13.0))
    assert not d.approved and any("total open risk" in r for r in d.reasons)
    assert system.risk.check_order(intent(system), ctx(system, equity=450, open_risk=5.0)).approved


def test_dynamic_correlation_blocks_duplicate_risk(system):
    system.risk.update_correlations({("BTC/USDT", "ETH/USDT"): 0.91, ("ETH/USDT", "BTC/USDT"): 0.91})
    d = system.risk.check_order(intent(system), ctx(system, held_symbols=["ETH/USDT"]))
    assert not d.approved and any("highly correlated" in r for r in d.reasons)
    system.risk.update_correlations({("BTC/USDT", "ETH/USDT"): 0.4, ("ETH/USDT", "BTC/USDT"): 0.4})
    assert system.risk.check_order(intent(system), ctx(system, held_symbols=["ETH/USDT"])).approved


def test_stock_leverage_always_forbidden(system):
    m = system.config.instruments["SPY"].to_market_info("SPY")
    i = intent(system, symbol="SPY", venue="stocks", asset_class=AssetClass.STOCK, price=500, reference_price=500,
               stop_price=490, quantity=0.1, leverage=1.5)
    d = system.risk.check_order(i, RiskContext(ExposureSnapshot(), m, 100, 100, system.clock.now()))
    assert not d.approved and any("forbidden for stocks" in r for r in d.reasons)


def test_crypto_leverage_rejected_without_permission(system):
    d = system.risk.check_order(intent(system, leverage=1.5, venue="crypto_perp"), ctx(system))
    assert not d.approved and any("leverage not permitted" in r for r in d.reasons)


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
    snap.position_qty[("crypto_momentum", "BTC/USDT")] = 0.001
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
    snap.position_qty[("crypto_momentum", "BTC/USDT")] = 0.001
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
    c = a.compute("crypto_momentum", [_pos("crypto_momentum", 0.001, 60000)], [_order("crypto_momentum", "c1", 0.0005, 60000)], 0)
    assert c.deployed == pytest.approx(60) and c.reserved == pytest.approx(30)
    assert c.available == pytest.approx(60)


def test_allocator_full_reinvestment(config):
    a = CapitalAllocator(config)
    assert a.compute("crypto_momentum", [], [], -20).effective_capital == pytest.approx(130)
    assert a.compute("crypto_momentum", [], [], +50).effective_capital == pytest.approx(200)  # 100% reinvestment
    assert a.compute("arbitrage", [], [], +50).effective_capital == 0  # unfunded strategy never trades
    no_reinvest = config.model_copy(deep=True)
    no_reinvest.capital.reinvest_profits = False
    assert CapitalAllocator(no_reinvest).compute("crypto_momentum", [], [], 50).effective_capital == 150


def test_allocator_never_negative_and_no_double_reservation(config):
    a = CapitalAllocator(config)
    c = a.compute("crypto_momentum", [_pos("crypto_momentum", 0.003, 60000)], [], 0)  # 180 deployed > 150
    assert c.available == 0.0
    assert a.reserve_inflight("crypto_momentum", "x1", 50, 100)
    assert not a.reserve_inflight("crypto_momentum", "x1", 50, 100)  # same order twice
    assert not a.reserve_inflight("crypto_momentum", "x2", 60, 100)  # would exceed available
    a.release_inflight("x1")
    assert a.reserve_inflight("crypto_momentum", "x2", 60, 100)


def test_allocator_reserve_and_other_strategies_isolated(config):
    a = CapitalAllocator(config)
    assert a.compute("unknown", [], [], 0).available == 0
    c = a.compute("stock_trend", [_pos("crypto_momentum", 0.002, 60000)], [], 0)
    assert c.available == 100  # crypto usage does not consume stock budget


def test_exposure_manager_counts_pending_buys(config):
    em = ExposureManager(config.correlation_group)
    snap = em.snapshot([_pos("crypto_momentum", 0.001, 60000)],
                       [_order("crypto_momentum", "a", 0.0005, 60000), _order("crypto_momentum", "b", 0.0002, 61000, Side.SELL)])
    assert snap.total == pytest.approx(90)
    assert snap.by_group["crypto_major"] == pytest.approx(90)
    assert snap.pending_sell_qty[("crypto_momentum", "BTC/USDT")] == pytest.approx(0.0002)
