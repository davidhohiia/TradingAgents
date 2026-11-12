"""Crypto Grid: signals, strategy safety properties and bot behaviour on the paper exchange."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from trinity.bots.crypto_grid.bot import CryptoGridBot
from trinity.bots.crypto_grid.signals import (
    adaptive_spacing,
    buy_levels,
    expected_net_profit,
    sell_target,
)
from trinity.bots.crypto_grid.strategy import GridStrategy, GridSymbolState, Lot
from trinity.config.settings import GridParams
from trinity.core.models import (
    MarketRegime,
    Mode,
    OrderBook,
    OrderRecord,
    OrderStatus,
    OrderType,
    Side,
)
from trinity.core.order_gateway import OrderGateway
from trinity.tests.conftest import build_system

T0 = datetime(2026, 3, 2, tzinfo=UTC)


def make_df(prices: list[float], spread: float = 0.002) -> pd.DataFrame:
    idx = [T0 + timedelta(minutes=15 * i) for i in range(len(prices))]
    rows = []
    prev = prices[0]
    for p in prices:
        rows.append({"open": prev, "high": max(prev, p) * (1 + spread), "low": min(prev, p) * (1 - spread),
                     "close": p, "volume": 100.0})
        prev = p
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx))


def ranging(n=200, base=60000.0, amp=0.01):
    return [base * (1 + amp * math.sin(i / 6)) for i in range(n)]


def crash(n=200, base=60000.0, step=-0.004):
    return [base * (1 + step) ** i for i in range(n)]


def mk_book(mid: float, ts: datetime) -> OrderBook:
    return OrderBook("BTC/USDT", [(mid * 0.9999, 5.0)], [(mid * 1.0001, 5.0)], ts, "test")


def market(config):
    return config.instruments["BTC/USDT"].to_market_info("BTC/USDT")


def test_adaptive_spacing_clamped():
    p = GridParams()
    assert adaptive_spacing(0.0001, p) == p.min_spacing_pct
    assert adaptive_spacing(1.0, p) == p.max_spacing_pct
    assert adaptive_spacing(0.02, p) == pytest.approx(0.02 * p.atr_spacing_multiplier)
    assert adaptive_spacing(float("nan"), p) == p.min_spacing_pct


def test_levels_below_mid_and_descending():
    lv = buy_levels(100.0, 0.01, 4, 0.01)
    assert lv == sorted(lv, reverse=True) and all(x < 100 for x in lv) and len(lv) == 4
    assert sell_target(99.0, 0.01, 0.01) == pytest.approx(99.99)


def test_expected_net_profit_accounts_for_fees_and_spread():
    np_ = expected_net_profit(100, 100.2, 1, 0.001, 0.001, 0.5)
    assert np_.gross == pytest.approx(0.2)
    assert np_.fees == pytest.approx(0.2002)
    assert np_.net < 0  # 0.2% spacing does not cover 0.2% fees + spread


def test_plan_ranging_places_fixed_size_buys_with_stop(config):
    s = GridStrategy(config.strategies.crypto_grid)
    df = make_df(ranging())
    st = GridSymbolState()
    now = df.index[-1].to_pydatetime()
    mid = df["close"].iloc[-1]
    plan = s.plan(df=df, book=mk_book(mid, now), now=now, st=st, position_qty=0.0, active=[],
                  market=market(config), max_risk_usd=1.5)
    assert plan.regime.regime in (MarketRegime.RANGING, MarketRegime.BULL)
    buys = [o for o in plan.orders if o.side == Side.BUY]
    assert buys, plan.reasons + plan.rejected_levels
    p = config.strategies.crypto_grid.grid
    for o in buys:
        assert o.order_type == OrderType.LIMIT and o.price < mid
        assert o.qty * o.price <= p.level_notional_usd + 1e-6  # fixed notional, never scaled up
        assert o.stop_price is not None and o.stop_price < o.price
        assert o.qty * (o.price - o.stop_price) <= 1.5 + 1e-9
        assert o.take_profit > o.price
    assert sum(o.qty * o.price for o in buys) <= p.max_inventory_usd_per_symbol + 1e-6


def test_plan_strong_bear_cancels_buys_and_blocks_new(config):
    s = GridStrategy(config.strategies.crypto_grid)
    df = make_df(crash())
    now = df.index[-1].to_pydatetime()
    active = [OrderRecord("b1", "crypto_grid", "crypto", "BTC/USDT", Mode.PAPER, Side.BUY, OrderType.LIMIT, 0.0003,
                          OrderStatus.OPEN, price=df["close"].iloc[-1] * 0.99)]
    st = GridSymbolState(buy_orders={"b1": 1.0})
    plan = s.plan(df=df, book=mk_book(df["close"].iloc[-1], now), now=now, st=st, position_qty=0.0,
                  active=active, market=market(config), max_risk_usd=1.5)
    assert plan.regime.regime in (MarketRegime.STRONG_BEAR, MarketRegime.BEAR, MarketRegime.HIGH_VOLATILITY)
    assert "b1" in plan.cancel
    assert not [o for o in plan.orders if o.side == Side.BUY]


def test_inventory_cap_blocks_accumulation(config):
    s = GridStrategy(config.strategies.crypto_grid)
    df = make_df(ranging())
    now = df.index[-1].to_pydatetime()
    mid = df["close"].iloc[-1]
    held = 59.0 / mid  # already holding ~59 USD (cap 60)
    plan = s.plan(df=df, book=mk_book(mid, now), now=now, st=GridSymbolState(), position_qty=held, active=[],
                  market=market(config), max_risk_usd=1.5)
    assert not [o for o in plan.orders if o.side == Side.BUY]
    assert any("inventory cap" in r for r in plan.rejected_levels)


def test_grid_stop_triggers_protective_exit_and_cooldown(config):
    s = GridStrategy(config.strategies.crypto_grid)
    df = make_df(ranging())
    now = df.index[-1].to_pydatetime()
    st = GridSymbolState(center=60000, spacing=0.006, atr=200, regime="ranging", stop_price=59000,
                         lots=[Lot("b1", 0.0003, 59500)])
    plan = s.plan(df=df, book=mk_book(58900, now), now=now, st=st, position_qty=0.0003, active=[],
                  market=market(config), max_risk_usd=1.5)
    assert plan.stop_triggered
    sells = [o for o in plan.orders if o.side == Side.SELL]
    assert len(sells) == 1 and sells[0].protective and sells[0].order_type == OrderType.MARKET
    assert st.cooldown_until is not None
    later = now + timedelta(minutes=10)
    plan2 = s.plan(df=df, book=mk_book(df["close"].iloc[-1], later), now=later, st=st, position_qty=0.0, active=[],
                   market=market(config), max_risk_usd=1.5)
    assert not [o for o in plan2.orders if o.side == Side.BUY]
    assert any("cooldown" in r for r in plan2.reasons)


def test_min_net_profit_rejects_levels(config):
    cfg = config.strategies.crypto_grid.model_copy(deep=True)
    cfg.grid.min_net_profit_usd = 10.0  # impossible with $20 levels
    s = GridStrategy(cfg)
    df = make_df(ranging())
    now = df.index[-1].to_pydatetime()
    plan = s.plan(df=df, book=mk_book(df["close"].iloc[-1], now), now=now, st=GridSymbolState(), position_qty=0.0,
                  active=[], market=market(config), max_risk_usd=1.5)
    assert not plan.orders and any("below minimum" in r for r in plan.rejected_levels)


def test_take_profit_sells_for_uncovered_lots(config):
    s = GridStrategy(config.strategies.crypto_grid)
    df = make_df(ranging())
    now = df.index[-1].to_pydatetime()
    st = GridSymbolState(lots=[Lot("b1", 0.0003, 59000), Lot("b2", 0.00004, 58800)])  # b2 is dust
    plan = s.plan(df=df, book=mk_book(df["close"].iloc[-1], now), now=now, st=st, position_qty=0.00034, active=[],
                  market=market(config), max_risk_usd=1.5, allow_new_buys=False)
    sells = [o for o in plan.orders if o.side == Side.SELL]
    assert any(o.lot_buy_cids == ["b1"] and o.price >= 59000 * 1.006 for o in sells)


# ---------------------------------------------------------------- bot on the paper exchange
def make_bot(system, config):
    gw = OrderGateway(system.engine, "crypto_grid", "crypto")
    return CryptoGridBot(config.strategies.crypto_grid, gateway=gw, market=system.crypto_market, repo=system.repo,
                         mode=Mode.PAPER, clock=system.clock)


async def test_bot_full_cycle_and_state_restore(system, config):
    bot = make_bot(system, config)
    await bot.start()
    for _ in range(400):
        assert await bot.safe_step(), bot.last_error
        system.clock.advance(60)
    orders = system.repo.list_orders(Mode.PAPER, limit=1000)
    assert any(o.side == Side.BUY for o in orders)
    buys = [o for o in orders if o.side == Side.BUY]
    assert all(o.quantity * (o.price or 0) <= 20.0 + 1e-6 for o in buys)  # never above fixed level size
    # restart: new bot instance restores lots from DB
    lots_before = sum(lot_.qty for st in bot.states.values() for lot_ in st.lots)
    bot2 = make_bot(system, config)
    assert sum(lot_.qty for st in bot2.states.values() for lot_ in st.lots) == pytest.approx(lots_before)


async def test_bot_does_not_accumulate_in_crash(tmp_path, config):
    s = build_system(tmp_path, config)
    for spec in s.crypto_market.specs.values():
        spec.annual_vol = 0.4
        spec.regimes = [(2.0, -60.0)]  # violent sell-off after 2 hours
    bot = make_bot(s, config)
    await bot.start()
    cap = config.strategies.crypto_grid.grid.max_inventory_usd_per_symbol
    max_cost = 0.0
    for _ in range(600):
        await bot.safe_step()
        s.clock.advance(60)
        for p in s.positions.all():
            max_cost = max(max_cost, p.cost_basis)
    assert max_cost <= cap + 1.0  # inventory cap held during the crash
    # no buy orders remain open in the bearish regime at the end
    assert not [o for o in s.repo.list_active_orders(Mode.PAPER) if o.side == Side.BUY]


async def test_bot_respects_risk_lock(system, config):
    system.risk.pause("test")
    bot = make_bot(system, config)
    await bot.start()
    for _ in range(5):
        await bot.safe_step()
        system.clock.advance(60)
    assert not [o for o in system.repo.list_orders(Mode.PAPER) if o.side == Side.BUY]


async def test_bot_reports_connection_error_on_outage(system, config):
    system.crypto_market.outages = [(system.clock.now(), system.clock.now() + timedelta(hours=1))]
    bot = make_bot(system, config)
    await bot.start()
    assert not await bot.safe_step()
    assert bot.status().value == "CONNECTION ERROR"
