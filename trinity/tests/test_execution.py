"""Execution engine + paper exchange + CCXT adapter with SIMULATED exchange responses."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import ccxt
import pytest

from trinity.core.models import (
    AssetClass,
    ExchangeOrder,
    Mode,
    OrderIntent,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
)
from trinity.exchanges.base_exchange import (
    AuthError,
    ExchangeUnavailable,
    InsufficientFunds,
    OrderRejected,
    RateLimited,
)
from trinity.exchanges.crypto_exchange import CCXTExchange
from trinity.tests.conftest import build_system


async def book(system, symbol="BTC/USDT"):
    return await system.crypto_market.fetch_order_book(symbol)


async def buy_intent(system, qty=0.0003, order_type=OrderType.MARKET, price=None, stop_frac=0.97, **kw):
    b = await book(system)
    ref = b.best_ask
    return OrderIntent(strategy_id="crypto_grid", venue="crypto", symbol="BTC/USDT", side=Side.BUY,
                       order_type=order_type, quantity=qty, reference_price=ref, market_data_ts=b.ts,
                       asset_class=AssetClass.CRYPTO, price=price,
                       stop_price=(price or ref) * stop_frac, **kw)


async def sell_intent(system, qty, order_type=OrderType.MARKET, price=None, **kw):
    b = await book(system)
    return OrderIntent(strategy_id="crypto_grid", venue="crypto", symbol="BTC/USDT", side=Side.SELL,
                       order_type=order_type, quantity=qty, reference_price=b.best_bid, market_data_ts=b.ts,
                       asset_class=AssetClass.CRYPTO, price=price, **kw)


# ---------------------------------------------------------------- paper market orders
async def test_market_buy_fills_with_fees_and_updates_position(system):
    res = await system.engine.submit(await buy_intent(system))
    assert res.accepted, res.reasons
    o = system.repo.get_order(res.client_order_id)
    assert o.status == OrderStatus.FILLED and o.filled_quantity == pytest.approx(0.0003)
    assert o.fee_usd == pytest.approx(0.0003 * o.avg_fill_price * 0.001, rel=1e-6)
    pos = system.positions.get("crypto_grid", "crypto", "BTC/USDT")
    assert pos.quantity == pytest.approx(0.0003)
    # cost basis includes fee
    assert pos.cost_basis == pytest.approx(0.0003 * o.avg_fill_price + o.fee_usd)
    assert system.crypto.balances["USDT"].free == pytest.approx(150 - pos.cost_basis, abs=1e-6)
    execs = system.repo.list_executions()
    assert len(execs) == 1 and execs[0]["mode"] == "paper"


async def test_round_trip_records_trade_with_net_pnl(system):
    await system.engine.submit(await buy_intent(system))
    res = await system.engine.submit(await sell_intent(system, 0.0003, reason="test exit"))
    assert res.accepted
    trades = system.repo.list_trades(Mode.PAPER)
    assert len(trades) == 1
    t = trades[0]
    # buying at ask and selling at bid with fees must lose money
    assert t["realized_pnl"] < 0 and t["exit_reason"] == "test exit"
    assert not system.positions.get("crypto_grid", "crypto", "BTC/USDT").is_open


async def test_market_order_partial_fill_when_depth_insufficient(tmp_path, config):
    s = build_system(tmp_path, config)
    s.crypto_market.specs["BTC/USDT"].level_qty_usd = 5.0  # very thin book
    s.crypto_market.specs["BTC/USDT"].depth_levels = 2
    res = await s.engine.submit(await buy_intent(s, qty=0.001, stop_frac=0.999))
    o = s.repo.get_order(res.client_order_id)
    assert o.status == OrderStatus.CANCELED  # IOC remainder canceled
    assert 0 < o.filled_quantity < 0.001
    assert s.positions.get("crypto_grid", "crypto", "BTC/USDT").quantity == pytest.approx(o.filled_quantity)


# ---------------------------------------------------------------- limit orders & partial fills
async def test_limit_order_rests_then_partially_then_fully_fills(system):
    b = await book(system)
    price = round(b.best_bid * 0.99, 2)
    res = await system.engine.submit(await buy_intent(system, qty=0.0003, order_type=OrderType.LIMIT, price=price))
    assert res.accepted
    o = system.repo.get_order(res.client_order_id)
    assert o.status == OrderStatus.OPEN and o.reserved_usd > 0
    # market touches the limit (not deep): partial fill (limit_fill_ratio = 0.5)
    system.crypto_market.set_price("BTC/USDT", price * (1 - 0.00012))  # best ask just below the limit
    system.clock.advance(1)
    await system.engine.sync_orders()
    o = system.repo.get_order(res.client_order_id)
    assert o.status == OrderStatus.PARTIALLY_FILLED and 0 < o.filled_quantity < 0.0003
    assert o.avg_fill_price == pytest.approx(price)
    # deep move through the price -> rest fills
    system.crypto_market.set_price("BTC/USDT", price * 0.99)
    system.clock.advance(1)
    await system.engine.sync_orders()
    o = system.repo.get_order(res.client_order_id)
    assert o.status == OrderStatus.FILLED and o.filled_quantity == pytest.approx(0.0003)
    assert o.reserved_usd == 0
    assert len(system.repo.list_executions()) == 2
    assert system.positions.get("crypto_grid", "crypto", "BTC/USDT").quantity == pytest.approx(0.0003)


async def test_cancel_releases_locked_funds(system):
    b = await book(system)
    res = await system.engine.submit(await buy_intent(system, order_type=OrderType.LIMIT,
                                                      price=round(b.best_bid * 0.98, 2)))
    assert system.crypto.balances["USDT"].used > 0
    assert await system.engine.cancel(res.client_order_id, "test")
    assert system.repo.get_order(res.client_order_id).status == OrderStatus.CANCELED
    assert system.crypto.balances["USDT"].used == pytest.approx(0, abs=1e-9)
    assert system.crypto.balances["USDT"].free == pytest.approx(150)


# ---------------------------------------------------------------- rejections
async def test_paper_rejects_insufficient_balance(tmp_path, config):
    s = build_system(tmp_path, config, crypto_balance=10)
    res = await s.engine.submit(await buy_intent(s, qty=0.0003))
    assert not res.accepted and any("venue rejected" in r for r in res.reasons)
    assert s.repo.get_order(res.client_order_id).status == OrderStatus.REJECTED


async def test_paper_rejects_below_min_and_closed_market(system):
    with pytest.raises(OrderRejected):
        await system.crypto.create_order(OrderRequest("x1", "BTC/USDT", Side.BUY, OrderType.LIMIT, 0.000001, 50000))
    with pytest.raises(InsufficientFunds):
        await system.crypto.create_order(OrderRequest("x2", "BTC/USDT", Side.SELL, OrderType.MARKET, 0.001))
    system.stocks.session_check = lambda now: False
    with pytest.raises(OrderRejected, match="market closed"):
        await system.stocks.create_order(OrderRequest("x3", "SPY", Side.BUY, OrderType.MARKET, 0.1))


async def test_risk_rejected_orders_never_reach_venue(system):
    res = await system.engine.submit(await buy_intent(system, qty=0.01))  # $600 > max order
    assert not res.accepted
    assert system.repo.get_order(res.client_order_id) is None
    assert system.crypto.orders == {}


# ---------------------------------------------------------------- duplicates
async def test_duplicate_client_order_id_rejected(system):
    i = await buy_intent(system)
    assert (await system.engine.submit(i)).accepted
    i2 = await buy_intent(system, client_order_id=i.client_order_id)
    res = await system.engine.submit(i2)
    assert not res.accepted and res.duplicate
    assert len(system.crypto.orders) == 1


async def test_duplicate_same_price_order_rejected(system):
    b = await book(system)
    px = round(b.best_bid * 0.99, 2)
    assert (await system.engine.submit(await buy_intent(system, order_type=OrderType.LIMIT, price=px))).accepted
    res = await system.engine.submit(await buy_intent(system, order_type=OrderType.LIMIT, price=px))
    assert not res.accepted and res.duplicate


async def test_paper_exchange_rejects_duplicate_client_id(system):
    await system.crypto.create_order(OrderRequest("dup1", "BTC/USDT", Side.BUY, OrderType.MARKET, 0.0002))
    with pytest.raises(OrderRejected, match="duplicate"):
        await system.crypto.create_order(OrderRequest("dup1", "BTC/USDT", Side.BUY, OrderType.MARKET, 0.0002))


async def test_fill_processing_is_idempotent(system):
    res = await system.engine.submit(await buy_intent(system))
    rec = system.repo.get_order(res.client_order_id)
    ex = await system.crypto.fetch_order("BTC/USDT", rec.exchange_order_id, rec.client_order_id)
    for _ in range(3):
        await system.engine._apply_update(rec, ex)  # same venue state re-applied
    assert len(system.repo.list_executions()) == 1
    assert system.positions.get("crypto_grid", "crypto", "BTC/USDT").quantity == pytest.approx(0.0003)


async def test_self_trade_between_strategies_rejected(system):
    await system.engine.submit(await buy_intent(system))
    b = await book(system)
    sell = await sell_intent(system, 0.0003, order_type=OrderType.LIMIT, price=round(b.best_ask * 1.01, 2))
    assert (await system.engine.submit(sell)).accepted
    other = OrderIntent(strategy_id="other", venue="crypto", symbol="BTC/USDT", side=Side.BUY,
                        order_type=OrderType.LIMIT, quantity=0.0002, reference_price=b.best_ask,
                        price=round(b.best_ask * 1.012, 2), stop_price=b.best_ask * 0.97, market_data_ts=b.ts,
                        asset_class=AssetClass.CRYPTO)
    res = await system.engine.submit(other)
    assert not res.accepted and any("self-trade" in r or "fat" in r or "above market" in r for r in res.reasons)


# ---------------------------------------------------------------- API errors / timeouts
async def test_submit_timeout_then_lookup_finds_order_no_duplicate(system):
    # venue receives the order but the response is lost
    original = system.crypto.create_order

    async def lossy(req):
        await original(req)
        raise ExchangeUnavailable("timeout after send")

    system.crypto.create_order = lossy
    res = await system.engine.submit(await buy_intent(system))
    assert res.accepted
    o = system.repo.get_order(res.client_order_id)
    assert o.status == OrderStatus.FILLED  # recovered via client-id lookup
    assert len(system.crypto.orders) == 1  # never re-sent


async def test_submit_timeout_unknown_outcome_resolved_by_sync(system):
    system.crypto.inject_failure("create_order", ExchangeUnavailable("timeout"))
    system.crypto.inject_failure("fetch_order", ExchangeUnavailable("timeout"))
    res = await system.engine.submit(await buy_intent(system))
    o = system.repo.get_order(res.client_order_id)
    assert o.status == OrderStatus.PENDING_SUBMIT and "unknown" in o.error
    system.clock.advance(180)
    await system.engine.sync_orders()
    o = system.repo.get_order(res.client_order_id)
    assert o.status == OrderStatus.FAILED  # never reached the venue
    assert o.reserved_usd == 0


async def test_repeated_api_failures_open_breaker_and_block_orders(system):
    b = await book(system)
    for i in range(system.config.risk.api_failure_threshold):
        system.crypto.inject_failure("create_order", ExchangeUnavailable("down"))
        system.crypto.inject_failure("fetch_order", ExchangeUnavailable("down"))
        px = round(b.best_bid * (0.99 - i * 0.001), 2)  # distinct prices (not duplicates)
        await system.engine.submit(await buy_intent(system, qty=0.0002, order_type=OrderType.LIMIT, price=px))
        system.clock.advance(1)
    assert not system.breakers.allow("crypto")
    res = await system.engine.submit(await buy_intent(system))
    assert not res.accepted and any("circuit breaker" in r for r in res.reasons)
    events = [r["event_type"] for r in system.repo.list_risk_events(50)]
    assert "API_CIRCUIT_OPEN" in events


# ---------------------------------------------------------------- paper / live separation
def test_live_engine_refuses_paper_venues(tmp_path, config):
    with pytest.raises(ValueError, match="live execution engine"):
        build_system(tmp_path, config, mode=Mode.LIVE)


async def test_paper_records_are_marked_paper(system):
    await system.engine.submit(await buy_intent(system))
    assert {r["mode"] for r in system.repo.query("SELECT mode FROM orders")} == {"paper"}
    assert system.repo.list_trades(Mode.LIVE) == []


# ---------------------------------------------------------------- integration: simulated CCXT responses
class FakeCCXT:
    """Simulated exchange HTTP layer with ccxt-shaped responses. No network access."""

    precisionMode = ccxt.TICK_SIZE  # noqa: N815
    has = {"fetchOpenOrders": True, "fetchClosedOrders": True, "fetchCanceledOrders": False}

    def __init__(self) -> None:
        self.orders: dict[str, dict] = {}
        self.n = 0
        self.fail_next: list[Exception] = []
        self.created_params: list[dict] = []
        self.fill_mode = "partial_then_full"

    def _maybe_fail(self):
        if self.fail_next:
            raise self.fail_next.pop(0)

    async def load_markets(self):
        return {"BTC/USDT": {"spot": True, "base": "BTC", "quote": "USDT", "active": True,
                             "precision": {"amount": 0.00001, "price": 0.01},
                             "limits": {"amount": {"min": 0.00001}, "cost": {"min": 5}},
                             "maker": 0.001, "taker": 0.001}}

    async def fetch_order_book(self, symbol, limit=None):
        self._maybe_fail()
        return {"bids": [[59990.0, 1.0]], "asks": [[60010.0, 1.0]], "timestamp": None}

    async def fetch_ticker(self, symbol):
        return {"bid": 59990.0, "ask": 60010.0, "last": 60000.0, "timestamp": None}

    async def create_order(self, symbol, type_, side, amount, price=None, params=None):
        self._maybe_fail()
        self.created_params.append(params or {})
        self.n += 1
        oid = str(1000 + self.n)
        o = {"id": oid, "clientOrderId": (params or {}).get("clientOrderId"), "symbol": symbol, "type": type_,
             "side": side, "amount": amount, "price": price, "status": "open", "filled": 0.0, "average": None,
             "fee": None, "timestamp": None, "_polls": 0}
        self.orders[oid] = o
        return dict(o)

    async def fetch_order(self, oid, symbol=None):
        self._maybe_fail()
        o = self.orders.get(oid)
        if o is None:
            raise ccxt.OrderNotFound(oid)
        if o["status"] == "open":
            o["_polls"] += 1
            if o["_polls"] == 1:
                o["filled"], o["average"] = o["amount"] / 2, o["price"]
                o["fee"] = {"cost": o["filled"] * o["price"] * 0.001, "currency": "USDT"}
            else:
                o["filled"], o["average"], o["status"] = o["amount"], o["price"], "closed"
                o["fee"] = {"cost": o["amount"] * o["price"] * 0.001, "currency": "USDT"}
        return dict(o)

    async def fetch_open_orders(self, symbol=None):
        return [dict(o) for o in self.orders.values() if o["status"] == "open"]

    async def fetch_closed_orders(self, symbol=None):
        return [dict(o) for o in self.orders.values() if o["status"] != "open"]

    async def cancel_order(self, oid, symbol=None):
        self.orders[oid]["status"] = "canceled"
        return dict(self.orders[oid])

    async def fetch_balance(self):
        return {"free": {"USDT": 140.0, "BTC": 0.001}, "used": {"USDT": 0.0}}

    async def close(self):
        pass


async def make_live_engine(tmp_path: Path, config, fake: FakeCCXT):
    from trinity.core.capital_allocator import CapitalAllocator
    from trinity.core.clock import SimClock
    from trinity.core.execution_engine import ExecutionEngine
    from trinity.core.position_manager import PositionManager
    from trinity.database.repository import Repository
    from trinity.risk.circuit_breaker import CircuitBreakerRegistry
    from trinity.risk.risk_manager import RiskManager
    from trinity.tests.conftest import START

    clock = SimClock(START)
    repo = Repository(tmp_path / "live.db")
    repo.migrate()
    ex = CCXTExchange("binance", api_key="TESTKEY123456", api_secret="TESTSECRET123456", client=fake)
    ex.name = "crypto"
    await ex.connect()
    breakers = CircuitBreakerRegistry(3, 60, clock)
    risk = RiskManager(config, repo, Mode.LIVE, breakers, clock)
    engine = ExecutionEngine(config=config, repo=repo, mode=Mode.LIVE, risk=risk, allocator=CapitalAllocator(config),
                             positions=PositionManager(repo, config, Mode.LIVE), venues={"crypto": ex},
                             breakers=breakers, clock=clock)
    return engine, clock


async def test_integration_simulated_exchange_partial_fills_live_path(tmp_path, config):
    fake = FakeCCXT()
    engine, clock = await make_live_engine(tmp_path, config, fake)
    i = OrderIntent(strategy_id="crypto_grid", venue="crypto", symbol="BTC/USDT", side=Side.BUY,
                    order_type=OrderType.LIMIT, quantity=0.0004, reference_price=60010.0, price=59500.0,
                    stop_price=57000.0, market_data_ts=clock.now(), asset_class=AssetClass.CRYPTO)
    res = await engine.submit(i)
    assert res.accepted, res.reasons
    assert fake.created_params[0]["clientOrderId"] == i.client_order_id  # unique client id sent
    await engine.sync_orders()
    o = engine.repo.get_order(i.client_order_id)
    assert o.status == OrderStatus.PARTIALLY_FILLED and o.filled_quantity == pytest.approx(0.0002)
    await engine.sync_orders()
    o = engine.repo.get_order(i.client_order_id)
    assert o.status == OrderStatus.FILLED and o.mode == Mode.LIVE
    assert len(engine.repo.list_executions(Mode.LIVE)) == 2
    assert o.fee_usd == pytest.approx(0.0004 * 59500 * 0.001)
    pos = engine.positions.get("crypto_grid", "crypto", "BTC/USDT")
    assert pos.quantity == pytest.approx(0.0004)
    rep = await engine.reconcile("crypto")
    assert rep.ok, rep.issues


async def test_integration_reconcile_detects_unknown_orders_and_missing_balance(tmp_path, config):
    fake = FakeCCXT()
    engine, clock = await make_live_engine(tmp_path, config, fake)
    await fake.create_order("BTC/USDT", "limit", "buy", 0.001, 50000.0, {"clientOrderId": "manual-order"})
    rep = await engine.reconcile("crypto")
    assert not rep.ok and any("unknown open order" in i for i in rep.issues)


def test_ccxt_error_mapping():
    from trinity.exchanges.crypto_exchange import _map_ccxt_error

    assert isinstance(_map_ccxt_error(ccxt.RequestTimeout("t")), ExchangeUnavailable)
    assert isinstance(_map_ccxt_error(ccxt.RateLimitExceeded("r")), RateLimited)
    assert isinstance(_map_ccxt_error(ccxt.InsufficientFunds("i")), InsufficientFunds)
    err = _map_ccxt_error(ccxt.AuthenticationError("invalid key ABCDEF123456"))
    assert isinstance(err, AuthError) and "ABCDEF" not in str(err)


async def test_ccxt_adapter_retries_transient_errors(tmp_path, config, monkeypatch):
    import trinity.exchanges.base_exchange as be

    async def no_sleep(_):
        return None

    monkeypatch.setattr(be.asyncio, "sleep", no_sleep)
    fake = FakeCCXT()
    ex = CCXTExchange("binance", client=fake)
    fake.fail_next = [ccxt.NetworkError("blip"), ccxt.RequestTimeout("slow")]
    ob = await ex.fetch_order_book("BTC/USDT")
    assert ob.best_bid == 59990.0
    fake.fail_next = [ccxt.NetworkError("x")] * 3
    with pytest.raises(ExchangeUnavailable):
        await ex.fetch_order_book("BTC/USDT")


async def test_ccxt_adapter_without_keys_cannot_trade():
    ex = CCXTExchange("binance", client=FakeCCXT())
    from trinity.exchanges.base_exchange import NotConfigured

    with pytest.raises(NotConfigured):
        await ex.create_order(OrderRequest("c", "BTC/USDT", Side.BUY, OrderType.MARKET, 0.001))


async def test_fee_charged_in_base_currency_credits_net_quantity(system):
    res = await system.engine.submit(await buy_intent(system, order_type=OrderType.LIMIT,
                                                      price=round((await book(system)).best_bid * 0.99, 2)))
    rec = system.repo.get_order(res.client_order_id)
    ex = ExchangeOrder(rec.client_order_id, rec.exchange_order_id, "BTC/USDT", Side.BUY, OrderType.LIMIT,
                       OrderStatus.FILLED, rec.quantity, rec.quantity, rec.price, rec.price,
                       fee=rec.quantity * 0.001, fee_currency="BTC")
    await system.engine._apply_update(rec, ex)
    pos = system.positions.get("crypto_grid", "crypto", "BTC/USDT")
    assert pos.quantity == pytest.approx(rec.quantity * 0.999)
    assert pos.cost_basis == pytest.approx(rec.quantity * rec.price, rel=1e-6)


async def test_pending_order_age_uses_clock(system):
    system.crypto.inject_failure("create_order", ExchangeUnavailable("timeout"))
    system.crypto.inject_failure("fetch_order", ExchangeUnavailable("timeout"))
    res = await system.engine.submit(await buy_intent(system))
    system.clock.advance(30)
    await system.engine.sync_orders()
    assert system.repo.get_order(res.client_order_id).status == OrderStatus.PENDING_SUBMIT
    system.clock.advance(timedelta(minutes=3).total_seconds())
    await system.engine.sync_orders()
    assert system.repo.get_order(res.client_order_id).status == OrderStatus.FAILED
