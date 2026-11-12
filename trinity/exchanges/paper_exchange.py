"""Paper (virtual) exchange.

Simulates execution against a real (or synthetic) market data source:
* market orders walk the order book (+ configurable extra slippage), IOC semantics
  -> partial fill if depth is insufficient, remainder canceled;
* limit orders: marketable part fills as taker, the rest rests (GTC) and fills as maker
  only when the market trades through the limit price, possibly in several partial fills;
* maker/taker fees charged in the quote currency;
* insufficient balance, min size, closed market and duplicate client ids are rejected;
* full state is serializable so paper results survive restarts.

Paper results are VIRTUAL. They are always stored with mode='paper'.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from trinity.core.clock import Clock, RealClock
from trinity.core.indicators import floor_to_step
from trinity.core.log import get_logger
from trinity.core.models import (
    AssetClass,
    Balance,
    Candle,
    ExchangeOrder,
    MarketInfo,
    OrderBook,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    Ticker,
)
from trinity.exchanges.base_exchange import (
    BaseExchange,
    ExchangeUnavailable,
    InsufficientFunds,
    MarketDataSource,
    OrderNotFound,
    OrderRejected,
)

log = get_logger(__name__)


@dataclass
class _PaperOrder:
    exchange_order_id: str
    client_order_id: str
    symbol: str
    side: Side
    order_type: OrderType
    quantity: float
    price: float | None
    status: OrderStatus
    filled: float = 0.0
    cost: float = 0.0  # sum(fill_qty * fill_price)
    fee: float = 0.0
    locked: float = 0.0  # quote (buy) or base (sell) still locked
    created_at: str = ""
    maker_fills: int = 0
    taker_fills: int = 0

    @property
    def remaining(self) -> float:
        return max(self.quantity - self.filled, 0.0)

    @property
    def average(self) -> float | None:
        return self.cost / self.filled if self.filled > 0 else None


class PaperExchange(BaseExchange):
    is_paper = True

    def __init__(
        self,
        name: str,
        data_source: MarketDataSource,
        markets: dict[str, MarketInfo],
        initial_balances: dict[str, float],
        *,
        clock: Clock | None = None,
        taker_slippage_bps: float = 5.0,
        limit_fill_ratio: float = 0.5,
        seed: int = 42,
        session_check: Callable[[datetime], bool] | None = None,
        on_change: Callable[[dict[str, Any]], None] | None = None,
        book_cache_seconds: float = 1.0,
    ) -> None:
        self.name = name
        self.asset_class = data_source.asset_class
        self.data = data_source
        self.markets = markets
        self.clock = clock or RealClock()
        self.taker_slippage = taker_slippage_bps / 10_000
        self.limit_fill_ratio = limit_fill_ratio
        self.rng = random.Random(seed)
        self.session_check = session_check
        self.on_change = on_change
        self.book_cache_seconds = book_cache_seconds
        self.balances: dict[str, Balance] = {a: Balance(a, float(v), 0.0) for a, v in initial_balances.items()}
        self.orders: dict[str, _PaperOrder] = {}
        self.by_client: dict[str, str] = {}
        self._counter = 0
        self._book_cache: dict[str, tuple[datetime, OrderBook]] = {}
        self._fail_next: dict[str, list[Exception]] = {}

    # ------------------------------------------------------------------ test hooks
    def inject_failure(self, method: str, exc: Exception, count: int = 1) -> None:
        self._fail_next.setdefault(method, []).extend([exc] * count)

    def _maybe_fail(self, method: str) -> None:
        queue = self._fail_next.get(method)
        if queue:
            raise queue.pop(0)

    # ------------------------------------------------------------------ persistence
    def to_state(self) -> dict[str, Any]:
        return {
            "counter": self._counter,
            "balances": {a: [b.free, b.used] for a, b in self.balances.items()},
            "orders": {k: vars(o) | {"side": o.side.value, "order_type": o.order_type.value,
                                     "status": o.status.value} for k, o in self.orders.items()},
        }

    def load_state(self, state: dict[str, Any]) -> None:
        self._counter = int(state.get("counter", 0))
        self.balances = {a: Balance(a, v[0], v[1]) for a, v in state.get("balances", {}).items()}
        self.orders = {}
        self.by_client = {}
        for k, raw in state.get("orders", {}).items():
            o = _PaperOrder(**{**raw, "side": Side(raw["side"]), "order_type": OrderType(raw["order_type"]),
                               "status": OrderStatus(raw["status"])})
            self.orders[k] = o
            self.by_client[o.client_order_id] = k

    def _changed(self) -> None:
        if self.on_change:
            self.on_change(self.to_state())

    # ------------------------------------------------------------------ market data passthrough
    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        return await self.data.fetch_ohlcv(symbol, timeframe, limit)

    async def fetch_order_book(self, symbol: str, depth: int = 20) -> OrderBook:
        return await self.data.fetch_order_book(symbol, depth)

    async def fetch_ticker(self, symbol: str) -> Ticker:
        return await self.data.fetch_ticker(symbol)

    async def is_market_open(self, now: datetime) -> bool:
        if self.session_check is not None:
            return self.session_check(now)
        return await self.data.is_market_open(now)

    def market_info(self, symbol: str) -> MarketInfo:
        if symbol not in self.markets:
            raise OrderRejected(f"unknown symbol {symbol} on {self.name}")
        return self.markets[symbol]

    async def _book(self, symbol: str) -> OrderBook:
        now = self.clock.now()
        cached = self._book_cache.get(symbol)
        if cached and (now - cached[0]) <= timedelta(seconds=self.book_cache_seconds):
            return cached[1]
        book = await self.data.fetch_order_book(symbol, 50)
        self._book_cache[symbol] = (now, book)
        return book

    def invalidate_cache(self) -> None:
        self._book_cache.clear()

    # ------------------------------------------------------------------ balances
    def _bal(self, asset: str) -> Balance:
        if asset not in self.balances:
            self.balances[asset] = Balance(asset, 0.0, 0.0)
        return self.balances[asset]

    async def fetch_balances(self) -> dict[str, Balance]:
        self._maybe_fail("fetch_balances")
        return {a: Balance(a, b.free, b.used) for a, b in self.balances.items()}

    # ------------------------------------------------------------------ orders
    def _to_exchange_order(self, o: _PaperOrder) -> ExchangeOrder:
        return ExchangeOrder(
            client_order_id=o.client_order_id, exchange_order_id=o.exchange_order_id, symbol=o.symbol,
            side=o.side, order_type=o.order_type, status=o.status, quantity=o.quantity, filled=o.filled,
            average_price=o.average, price=o.price, fee=o.fee, fee_currency=self.markets[o.symbol].quote,
            ts=self.clock.now(), raw_status=o.status.value,
            is_maker=o.maker_fills > 0 and o.taker_fills == 0 if o.filled > 0 else None,
        )

    async def create_order(self, req: OrderRequest) -> ExchangeOrder:
        self._maybe_fail("create_order")
        if req.client_order_id in self.by_client:
            raise OrderRejected(f"duplicate client order id {req.client_order_id}")
        m = self.market_info(req.symbol)
        now = self.clock.now()
        if not await self.is_market_open(now):
            raise OrderRejected(f"market closed for {req.symbol}")
        qty = floor_to_step(req.quantity, m.qty_step)
        if qty <= 0 or qty < m.min_qty - 1e-12:
            raise OrderRejected(f"quantity {req.quantity} below minimum {m.min_qty}")
        if req.order_type == OrderType.LIMIT and (req.price is None or req.price <= 0):
            raise OrderRejected("limit order requires a positive price")
        book = await self._book(req.symbol)
        ref = req.price if req.order_type == OrderType.LIMIT else (
            book.best_ask if req.side == Side.BUY else book.best_bid)
        if ref is None:
            raise ExchangeUnavailable(f"no liquidity in book for {req.symbol}")
        if qty * ref < m.min_notional - 1e-9:
            raise OrderRejected(f"notional {qty * ref:.4f} below minimum {m.min_notional}")

        quote, base = self._bal(m.quote), self._bal(m.base)
        # Balance check (worst case for market buys: walk the book with slippage)
        if req.side == Side.BUY:
            if req.order_type == OrderType.MARKET:
                est_px = _vwap_for(book.asks, qty) or ref
                need = qty * est_px * (1 + self.taker_slippage) * (1 + m.taker_fee)
            else:
                need = qty * req.price * (1 + max(m.maker_fee, m.taker_fee))  # type: ignore[operator]
            if need > quote.free + 1e-9:
                raise InsufficientFunds(f"insufficient {m.quote}: need {need:.4f}, free {quote.free:.4f}")
        elif qty > base.free + 1e-12:
            raise InsufficientFunds(f"insufficient {m.base}: need {qty}, free {base.free}")

        self._counter += 1
        oid = f"P{self._counter:08d}"
        o = _PaperOrder(oid, req.client_order_id, req.symbol, req.side, req.order_type, qty, req.price,
                        OrderStatus.OPEN, created_at=now.isoformat())
        self.orders[oid] = o
        self.by_client[req.client_order_id] = oid
        if req.order_type == OrderType.LIMIT:
            # lock funds for the resting part
            if req.side == Side.BUY:
                o.locked = qty * req.price * (1 + max(m.maker_fee, m.taker_fee))  # type: ignore[operator]
                quote.free -= o.locked
                quote.used += o.locked
            else:
                o.locked = qty
                base.free -= qty
                base.used += qty
            self._fill_taker(o, m, book, limit=req.price)
        else:
            self._fill_taker(o, m, book, limit=None)
            # IOC: anything not filled is canceled
            if o.remaining > 1e-12:
                o.status = OrderStatus.CANCELED
        self._update_status(o)
        self._changed()
        return self._to_exchange_order(o)

    def _apply_fill(self, o: _PaperOrder, m: MarketInfo, qty: float, price: float, fee_rate: float, maker: bool) -> None:
        if qty <= 0:
            return
        quote, base = self._bal(m.quote), self._bal(m.base)
        cost = qty * price
        fee = cost * fee_rate
        if o.side == Side.BUY:
            if o.order_type == OrderType.LIMIT:
                release = min(o.locked, qty * (o.price or price) * (1 + max(m.maker_fee, m.taker_fee)))
                o.locked -= release
                quote.used -= release
                quote.free += release
            quote.free -= cost + fee
            base.free += qty
        else:
            if o.order_type == OrderType.LIMIT:
                release = min(o.locked, qty)
                o.locked -= release
                base.used -= release
            else:
                base.free -= qty
            quote.free += cost - fee
        o.filled += qty
        o.cost += cost
        o.fee += fee
        if maker:
            o.maker_fills += 1
        else:
            o.taker_fills += 1

    def _fill_taker(self, o: _PaperOrder, m: MarketInfo, book: OrderBook, limit: float | None) -> None:
        levels = book.asks if o.side == Side.BUY else book.bids
        remaining = o.remaining
        for price, amount in levels:
            if remaining <= 1e-12:
                break
            if limit is not None and ((o.side == Side.BUY and price > limit) or (o.side == Side.SELL and price < limit)):
                break
            take = floor_to_step(min(remaining, amount), m.qty_step)
            if take <= 0:
                continue
            slip = self.taker_slippage if limit is None else 0.0
            px = price * (1 + slip) if o.side == Side.BUY else price * (1 - slip)
            if o.side == Side.BUY and o.order_type == OrderType.MARKET:
                quote = self._bal(m.quote)
                affordable = floor_to_step(quote.free / (px * (1 + m.taker_fee)), m.qty_step)
                take = min(take, affordable)
                if take <= 0:
                    break
            self._apply_fill(o, m, take, px, m.taker_fee, maker=False)
            remaining = o.remaining

    async def _match_resting(self, o: _PaperOrder) -> None:
        if o.status not in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED) or o.order_type != OrderType.LIMIT:
            return
        m = self.markets[o.symbol]
        book = await self._book(o.symbol)
        assert o.price is not None
        if o.side == Side.BUY:
            crossing = [(p, a) for p, a in book.asks if p <= o.price]
            deep = book.best_ask is not None and book.best_ask < o.price * (1 - 0.001)
        else:
            crossing = [(p, a) for p, a in book.bids if p >= o.price]
            deep = book.best_bid is not None and book.best_bid > o.price * (1 + 0.001)
        if not crossing:
            return
        available = sum(a for _, a in crossing)
        target = o.remaining if deep else o.remaining * self.limit_fill_ratio
        qty = floor_to_step(min(o.remaining, available, max(target, m.qty_step)), m.qty_step)
        if o.remaining - qty < max(m.qty_step, m.min_qty):
            qty = o.remaining if available >= o.remaining else qty
        if qty <= 0:
            return
        # Resting order fills at its own limit price (maker).
        self._apply_fill(o, m, qty, o.price, m.maker_fee, maker=True)
        self._update_status(o)

    async def _safe_match(self, o: _PaperOrder) -> None:
        """Matching needs market data; if it is unavailable the order state is simply unchanged."""
        try:
            await self._match_resting(o)
        except ExchangeUnavailable as exc:
            log.debug("paper matching skipped for %s: %s", o.symbol, exc)

    def _update_status(self, o: _PaperOrder) -> None:
        if o.status in (OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
            return
        if o.remaining <= 1e-12:
            o.status = OrderStatus.FILLED
        elif o.filled > 0:
            o.status = OrderStatus.PARTIALLY_FILLED
        else:
            o.status = OrderStatus.OPEN

    async def match_all(self) -> None:
        """Run one matching pass for every resting order."""
        for o in list(self.orders.values()):
            if o.status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED):
                try:
                    await self._match_resting(o)
                except ExchangeUnavailable as exc:
                    log.debug("paper matching skipped for %s: %s", o.symbol, exc)
        self._changed()

    def _find(self, exchange_order_id: str | None, client_order_id: str | None) -> _PaperOrder | None:
        if exchange_order_id and exchange_order_id in self.orders:
            return self.orders[exchange_order_id]
        if client_order_id and client_order_id in self.by_client:
            return self.orders[self.by_client[client_order_id]]
        return None

    async def cancel_order(self, symbol: str, exchange_order_id: str | None, client_order_id: str | None) -> ExchangeOrder:
        self._maybe_fail("cancel_order")
        o = self._find(exchange_order_id, client_order_id)
        if o is None:
            raise OrderNotFound(f"order {exchange_order_id or client_order_id} not found")
        await self._safe_match(o)  # fills that happened before the cancel arrived
        if o.status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED):
            m = self.markets[o.symbol]
            if o.side == Side.BUY:
                q = self._bal(m.quote)
                q.used -= o.locked
                q.free += o.locked
            else:
                b = self._bal(m.base)
                b.used -= o.locked
                b.free += o.locked
            o.locked = 0.0
            o.status = OrderStatus.CANCELED
        self._changed()
        return self._to_exchange_order(o)

    async def fetch_order(self, symbol: str, exchange_order_id: str | None, client_order_id: str | None) -> ExchangeOrder | None:
        self._maybe_fail("fetch_order")
        o = self._find(exchange_order_id, client_order_id)
        if o is None:
            return None
        await self._safe_match(o)
        self._changed()
        return self._to_exchange_order(o)

    async def fetch_open_orders(self, symbol: str | None = None) -> list[ExchangeOrder]:
        self._maybe_fail("fetch_open_orders")
        return [
            self._to_exchange_order(o) for o in self.orders.values()
            if o.status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED) and (symbol is None or o.symbol == symbol)
        ]

    def capabilities(self) -> dict[str, Any]:
        return {"client_order_id": True, "fractional": True, "spot_only": True, "paper": True}


def _vwap_for(levels: list[tuple[float, float]], qty: float) -> float | None:
    remaining, cost = qty, 0.0
    for price, amount in levels:
        take = min(remaining, amount)
        cost += take * price
        remaining -= take
        if remaining <= 1e-12:
            return cost / qty
    return None


def default_paper_asset_class(markets: dict[str, MarketInfo]) -> AssetClass:
    return next(iter(markets.values())).asset_class if markets else AssetClass.CRYPTO
