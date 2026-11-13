"""Limited leverage: default OFF, 60-day gate, liquidation/funding checks, isolated-margin adapter (mocked)."""

from __future__ import annotations

from datetime import timedelta

import ccxt
import pytest

from trinity.config.settings import LEVERAGE_ACKNOWLEDGEMENT_PHRASE, LeverageConfig
from trinity.core.models import AssetClass, Mode, OrderIntent, OrderRequest, OrderType, Side
from trinity.database.repository import Repository
from trinity.exchanges.base_exchange import OrderRejected
from trinity.exchanges.crypto_exchange import CCXTDerivativesExchange
from trinity.risk.leverage import (
    APPROVAL_PHRASE,
    LeverageGuard,
    funding_cost,
    liquidation_price_long,
    liquidation_price_short,
)
from trinity.tests.conftest import START
from trinity.tests.test_recovery import settings_for


def test_liquidation_and_funding_math():
    assert liquidation_price_long(100, 1.0, 0.005) == 0.0  # spot is never liquidated
    assert liquidation_price_long(100, 2.0, 0.005) == pytest.approx(50.5)
    assert liquidation_price_long(100, 1.5, 0.005) == pytest.approx(100 * (1 - 1 / 1.5 + 0.005))
    assert liquidation_price_short(100, 2.0, 0.005) == pytest.approx(149.5)
    assert funding_cost(1000, 0.0001, 24) == pytest.approx(0.3)


def guard(tmp_path, config, *, enabled=True, env=True, ack=True, mode=Mode.LIVE, repo=None):
    cfg = config.leverage.model_copy(update={"enabled": enabled})
    st = settings_for(tmp_path, live_leverage_enabled=env,
                      leverage_acknowledgement=LEVERAGE_ACKNOWLEDGEMENT_PHRASE if ack else "")
    if repo is None:
        repo = Repository(tmp_path / "lev.db")
        repo.migrate()
    return LeverageGuard(cfg, st, repo, mode), repo


def seed_live_history(repo, days, pnl):
    repo.add_equity_snapshot(START - timedelta(days=days), Mode.LIVE, 500, 500, 0, 0, 0, 0)
    repo.upsert_trade_fill(strategy_id="crypto_momentum", venue="crypto", symbol="BTC/USDT", mode=Mode.LIVE,
                           quantity=0.001, entry_price=1, exit_price=1, entry_time=START, exit_time=START,
                           fees_usd=0, realized_pnl=pnl, exit_reason="", exit_client_order_id="x", is_virtual=False)


def test_default_config_is_off(config, tmp_path):
    assert config.leverage.enabled is False
    g = LeverageGuard(config.leverage, settings_for(tmp_path), Repository(":memory:"), Mode.LIVE)
    g.repo.migrate()
    rep = g.permission(START, tests_ok=True)
    assert not rep.permitted and any("enabled=false" in r for r in rep.reasons)


def test_never_in_paper_mode(tmp_path, config):
    g, repo = guard(tmp_path, config, mode=Mode.PAPER)
    seed_live_history(repo, 90, 50)
    assert any("paper" in r for r in g.permission(START, True).reasons)


def test_requires_60_days_successful_live_trading_and_approval(tmp_path, config):
    g, repo = guard(tmp_path, config)
    seed_live_history(repo, 59, 50)
    assert any("59 days" in r for r in g.permission(START, True).reasons)
    (tmp_path / "b").mkdir()
    (tmp_path / "c").mkdir()
    g2, repo2 = guard(tmp_path / "b", config)
    seed_live_history(repo2, 61, -5)
    assert any("not successful" in r for r in g2.permission(START, True).reasons)
    g3, repo3 = guard(tmp_path / "c", config)
    seed_live_history(repo3, 61, 50)
    rep = g3.permission(START, True)
    assert rep.reasons == ["no local approval record (scripts/leverage.py --approve)"]
    assert not g3.record_approval("ok", START).permitted
    assert not g3.permission(START, tests_ok=False).permitted  # tests must pass
    rep = g3.record_approval(APPROVAL_PHRASE, START)
    assert rep.permitted and rep.approved_max == 1.5


def _permitted_guard(tmp_path, config):
    g, repo = guard(tmp_path, config)
    seed_live_history(repo, 61, 50)
    assert g.record_approval(APPROVAL_PHRASE, START).permitted
    return g, repo


def lev_intent(**kw):
    base = dict(strategy_id="crypto_momentum", venue="crypto_perp", symbol="BTC/USDT", side=Side.BUY, quality_score=80,
                order_type=OrderType.LIMIT, quantity=0.001, reference_price=60000, price=60000, stop_price=58000,
                market_data_ts=START, asset_class=AssetClass.CRYPTO, leverage=1.5)
    base.update(kw)
    return OrderIntent(**base)


def test_per_order_checks_when_permitted(tmp_path, config):
    g, _ = _permitted_guard(tmp_path, config)
    assert g.check_intent(lev_intent(), START, 0.0001, True) == []
    assert any("> allowed" in r for r in g.check_intent(lev_intent(leverage=2.0), START, 0.0001, True))
    assert any("not allowed for SOL" in r for r in g.check_intent(lev_intent(symbol="SOL/USDT"), START, 0.0001, True))
    # liquidation ~ 20300 at 1.5x; stop must be above liq + 50% of the distance (~40150)
    assert any("too close to liquidation" in r
               for r in g.check_intent(lev_intent(stop_price=35000), START, 0.0001, True))
    assert any("funding rate" in r for r in g.check_intent(lev_intent(), START, 0.002, True))
    assert any("unknown" in r for r in g.check_intent(lev_intent(), START, None, True))
    assert any("stop loss" in r for r in g.check_intent(lev_intent(stop_price=None), START, 0.0001, True))


def test_leverage_cannot_be_raised_without_new_approval(tmp_path, config):
    g, repo = _permitted_guard(tmp_path, config)
    raised = LeverageGuard(g.cfg.model_copy(update={"max_leverage": 2.0}), g.settings, repo, Mode.LIVE)
    rep = raised.permission(START, True)
    assert not rep.permitted and any("cannot be increased automatically" in r for r in rep.reasons)


def test_absolute_cap():
    with pytest.raises(Exception):  # noqa: B017
        LeverageConfig(max_leverage=2.5)


# ---------------------------------------------------------------- isolated-margin adapter (mocked CCXT)
class FakePerp:
    precisionMode = ccxt.TICK_SIZE  # noqa: N815
    has: dict = {}

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def load_markets(self):
        return {"BTC/USDT:USDT": {"swap": True, "linear": True, "base": "BTC", "quote": "USDT", "contractSize": 0.01,
                                  "precision": {"amount": 1, "price": 0.1},
                                  "limits": {"amount": {"min": 1}, "cost": {"min": 5}}, "maker": 0.0002,
                                  "taker": 0.0005}}

    async def set_margin_mode(self, mode, symbol):
        self.calls.append(("margin", mode, symbol))

    async def set_leverage(self, lev, symbol):
        self.calls.append(("leverage", lev, symbol))

    async def fetch_funding_rate(self, symbol):
        return {"fundingRate": 0.0001, "interval": "8h"}

    async def create_order(self, symbol, type_, side, amount, price=None, params=None):
        self.calls.append(("order", symbol, side, amount, params))
        return {"id": "1", "clientOrderId": params["clientOrderId"], "symbol": symbol, "type": type_, "side": side,
                "amount": amount, "price": price, "status": "open", "filled": 0, "average": None}

    async def close(self):
        pass


async def test_derivatives_adapter_sets_isolated_and_converts_contracts():
    fake = FakePerp()
    ex = CCXTDerivativesExchange("binance", allowed_symbols=["BTC/USDT", "ETH/USDT"], client=fake,
                                 api_key="k" * 10, api_secret="s" * 10)
    await ex.connect()
    m = ex.market_info("BTC/USDT")
    assert m.qty_step == pytest.approx(0.01) and m.min_qty == pytest.approx(0.01)
    with pytest.raises(OrderRejected):  # leverage must be prepared first
        await ex.create_order(OrderRequest("c1", "BTC/USDT", Side.BUY, OrderType.LIMIT, 0.02, 60000))
    with pytest.raises(OrderRejected):
        await ex.prepare_leverage("BTC/USDT", 3.0)  # above the absolute cap
    await ex.prepare_leverage("BTC/USDT", 1.5)
    assert ("margin", "isolated", "BTC/USDT:USDT") in fake.calls and ("leverage", 1.5, "BTC/USDT:USDT") in fake.calls
    o = await ex.create_order(OrderRequest("c2", "BTC/USDT", Side.BUY, OrderType.LIMIT, 0.02, 60000))
    order_call = [c for c in fake.calls if c[0] == "order"][0]
    assert order_call[3] == pytest.approx(2)  # 0.02 BTC = 2 contracts of 0.01
    assert order_call[4]["marginMode"] == "isolated"
    assert o.quantity == pytest.approx(0.02) and o.symbol == "BTC/USDT"
    assert await ex.fetch_funding_rate("BTC/USDT") == pytest.approx(0.0001)
    with pytest.raises(OrderRejected):
        await ex.prepare_leverage("SOL/USDT", 1.5)


async def test_engine_rejects_leveraged_intent_on_spot_paper_venue(system):
    b = await system.crypto_market.fetch_order_book("BTC/USDT")
    res = await system.engine.submit(lev_intent(venue="crypto", reference_price=b.best_ask, price=b.best_ask,
                                                stop_price=b.best_ask * 0.97, market_data_ts=b.ts))
    assert not res.accepted and any("leverage" in r for r in res.reasons)
