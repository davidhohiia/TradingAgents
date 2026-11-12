"""Arbitrage scanner math, rejection rules, virtual simulator and scan-only bot."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trinity.bots.arbitrage.bot import ArbitrageBot
from trinity.bots.arbitrage.scanner import ArbitrageScanner, VenueLimits, vwap_for_qty
from trinity.bots.arbitrage.simulator import ArbitrageSimulator
from trinity.config.settings import ArbitrageConfig
from trinity.core.clock import SimClock
from trinity.core.models import AssetClass, BotStatus, Mode, OrderBook
from trinity.database.repository import Repository
from trinity.exchanges.base_exchange import ExchangeUnavailable
from trinity.exchanges.simulated_market import SimulatedMarket, SymbolSpec

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)


def cfg(**kw) -> ArbitrageConfig:
    base = dict(trade_size_usd=100, min_net_profit_usd=0.10, min_net_profit_bps=5, slippage_bps=5,
                transfer_fees={"BTC": 0.0, "USDT": 0.0}, taker_fees={"default": 0.001}, assumed_latency_ms=500,
                volatility_per_second_bps=2.0, max_execution_risk_usd=0.5)
    base.update(kw)
    return ArbitrageConfig(**base)


def ob(venue, bid, ask, qty=10.0, ts=NOW):
    return OrderBook("BTC/USDT", [(bid, qty)], [(ask, qty)], ts, venue)


def test_vwap_walks_book():
    vwap, filled = vwap_for_qty([(100, 1), (101, 1)], 1.5)
    assert vwap == pytest.approx((100 + 50.5) / 1.5) and filled == 1.5
    assert vwap_for_qty([(100, 1)], 2)[0] is None


def test_net_profit_formula_matches_spec():
    c = cfg()
    opps = ArbitrageScanner(c).evaluate("BTC/USDT", {"a": ob("a", 59900, 60000), "b": ob("b", 60600, 60700)}, NOW)
    o = next(x for x in opps if x.buy_venue == "a" and x.sell_venue == "b")
    qty = 100 / 60000
    buy_cost, proceeds = qty * 60000, qty * 60600
    fees = buy_cost * 0.001 + proceeds * 0.001
    slip = (buy_cost + proceeds) * 5 / 10_000
    latency = buy_cost * 2 / 10_000 * (0.5 ** 0.5)
    assert o.net_profit == pytest.approx(proceeds - buy_cost - fees - slip - latency)
    assert o.accepted


def test_rejects_negative_profit_when_fees_exceed_spread():
    opps = ArbitrageScanner(cfg()).evaluate("BTC/USDT", {"a": ob("a", 59990, 60000), "b": ob("b", 60050, 60060)}, NOW)
    assert all(not o.accepted for o in opps)
    assert any("negative" in r for o in opps for r in o.reasons)


def test_rejects_insufficient_depth_stale_unavailable_min_size():
    c = cfg()
    s = ArbitrageScanner(c, {("b", "BTC/USDT"): VenueLimits(min_qty=1.0)})
    thin = s.evaluate("BTC/USDT", {"a": ob("a", 59900, 60000, qty=0.0001), "b": ob("b", 60600, 60700)}, NOW)
    assert any("depth" in r for r in next(o for o in thin if o.buy_venue == "a").reasons)
    stale = s.evaluate("BTC/USDT", {"a": ob("a", 59900, 60000, ts=NOW - timedelta(seconds=30)),
                                    "b": ob("b", 60600, 60700)}, NOW)
    assert any("stale" in r for r in stale[0].reasons)
    down = s.evaluate("BTC/USDT", {"a": ExchangeUnavailable("x"), "b": ob("b", 60600, 60700)}, NOW)
    assert all(any("unavailable" in r for r in o.reasons) for o in down)
    small = s.evaluate("BTC/USDT", {"a": ob("a", 59900, 60000), "b": ob("b", 60600, 60700)}, NOW)
    assert any("minimum order size" in r for r in next(o for o in small if o.buy_venue == "a").reasons)


def test_rejects_execution_risk_and_transfer_costs():
    risky = ArbitrageScanner(cfg(assumed_latency_ms=10_000_000, volatility_per_second_bps=50)).evaluate(
        "BTC/USDT", {"a": ob("a", 59900, 60000), "b": ob("b", 60600, 60700)}, NOW)
    assert any("execution risk" in r for o in risky for r in o.reasons)
    costly = ArbitrageScanner(cfg(transfer_fees={"BTC": 0.01, "USDT": 1}, transfer_amortization_trades=1)).evaluate(
        "BTC/USDT", {"a": ob("a", 59900, 60000), "b": ob("b", 60600, 60700)}, NOW)
    assert not next(o for o in costly if o.buy_venue == "a").accepted


def test_simulator_inventory_and_pnl():
    sim = ArbitrageSimulator(600, ["a", "b"], n_bases=1)  # 150 USD per asset per venue
    sim.seed_inventory("BTC", 60000, 2)
    opps = ArbitrageScanner(cfg()).evaluate("BTC/USDT", {"a": ob("a", 59900, 60000), "b": ob("b", 60600, 60700)}, NOW)
    o = next(x for x in opps if x.accepted)
    assert sim.try_execute(o)
    assert sim.book.trades == 1 and sim.book.net_pnl == pytest.approx(o.net_profit)
    assert not sim.try_execute(next(x for x in opps if not x.accepted))


def test_config_only_scan_mode():
    with pytest.raises(Exception):  # noqa: B017
        ArbitrageConfig(mode="live")


async def test_bot_scan_only_never_uses_execution(tmp_path):
    clock = SimClock(NOW)
    repo = Repository(tmp_path / "a.db")
    repo.migrate()
    srcs = {v: SimulatedMarket(v, AssetClass.CRYPTO, [SymbolSpec("BTC/USDT", 60000, "15m"),
                                                      SymbolSpec("ETH/USDT", 3000, "15m")], clock, seed=i)
            for i, v in enumerate(["x", "y", "z"])}
    bot = ArbitrageBot(cfg(), srcs, repo=repo, mode=Mode.PAPER, clock=clock)
    assert bot.gateway is None  # structurally cannot place orders
    await bot.start()
    for _ in range(5):
        assert await bot.safe_step()
        clock.advance(10)
    assert bot.status() == BotStatus.SCAN_ONLY
    assert repo.list_arbitrage_opportunities(100)
    assert repo.query("SELECT COUNT(*) FROM orders")[0][0] == 0
    srcs["x"].outages = srcs["y"].outages = srcs["z"].outages = [(clock.now(), clock.now() + timedelta(hours=1))]
    assert not await bot.safe_step()
    assert bot.status() == BotStatus.CONNECTION_ERROR


def test_data_delay_control_rejects_slow_snapshots():
    opps = ArbitrageScanner(cfg()).evaluate("BTC/USDT", {"a": ob("a", 59900, 60000), "b": ob("b", 60600, 60700)}, NOW,
                                           latencies_ms={"a": 50, "b": 4000})
    assert all(any("data delay on b" in r for r in o.reasons) for o in opps)
    ok = ArbitrageScanner(cfg()).evaluate("BTC/USDT", {"a": ob("a", 59900, 60000), "b": ob("b", 60600, 60700)}, NOW,
                                         latencies_ms={"a": 50, "b": 60})
    assert any(o.accepted for o in ok)
