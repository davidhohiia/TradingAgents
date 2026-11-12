"""Crypto exchange adapter built on CCXT (async).

Spot only. No withdrawal / transfer functions are exposed by this adapter.
Market data endpoints are public and work without API keys; trading requires keys
with TRADE permission only (disable withdrawals on the exchange side).

The CCXT unified API is documented at https://docs.ccxt.com. Exchange-specific
behaviour (client order id support, fee currency, minimums) differs between venues
and must be verified against each exchange's official API documentation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

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
    AuthError,
    BaseExchange,
    ExchangeError,
    ExchangeUnavailable,
    InsufficientFunds,
    NotConfigured,
    OrderNotFound,
    OrderRejected,
    RateLimited,
    with_retries,
)

log = get_logger(__name__)

_CCXT_STATUS = {
    "open": OrderStatus.OPEN,
    "closed": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "cancelled": OrderStatus.CANCELED,
    "expired": OrderStatus.EXPIRED,
    "rejected": OrderStatus.REJECTED,
}


def _map_ccxt_error(exc: Exception) -> Exception:
    import ccxt  # local import keeps module importable without ccxt for docs

    if isinstance(exc, ccxt.RateLimitExceeded | ccxt.DDoSProtection):
        return RateLimited(str(exc))
    if isinstance(exc, ccxt.AuthenticationError | ccxt.PermissionDenied | ccxt.AccountSuspended):
        return AuthError(type(exc).__name__)  # never echo raw message (may include key fragments)
    if isinstance(exc, ccxt.InsufficientFunds):
        return InsufficientFunds(str(exc))
    if isinstance(exc, ccxt.OrderNotFound):
        return OrderNotFound(str(exc))
    if isinstance(exc, ccxt.InvalidOrder | ccxt.BadRequest | ccxt.BadSymbol):
        return OrderRejected(str(exc))
    if isinstance(exc, ccxt.NetworkError | ccxt.ExchangeNotAvailable | ccxt.RequestTimeout):
        return ExchangeUnavailable(str(exc))
    if isinstance(exc, ccxt.BaseError):
        return ExchangeError(str(exc))
    return exc


class CCXTExchange(BaseExchange):
    """Live (or public-data-only) CCXT adapter."""

    def __init__(
        self,
        exchange_id: str,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        api_password: str | None = None,
        timeout_ms: int = 15_000,
        instruments_fallback: dict[str, MarketInfo] | None = None,
        client: Any = None,
    ) -> None:
        self.name = exchange_id
        self.asset_class = AssetClass.CRYPTO
        self.exchange_id = exchange_id
        self.has_credentials = bool(api_key and api_secret)
        self._fallback = instruments_fallback or {}
        self._markets: dict[str, MarketInfo] = {}
        if client is not None:
            self.client = client  # injected (tests)
        else:
            import ccxt.async_support as ccxt_async

            if not hasattr(ccxt_async, exchange_id):
                raise NotConfigured(f"CCXT has no exchange '{exchange_id}'")
            cfg: dict[str, Any] = {
                "enableRateLimit": True,
                "timeout": timeout_ms,
                "options": {"defaultType": "spot"},
            }
            if api_key and api_secret:
                cfg["apiKey"] = api_key
                cfg["secret"] = api_secret
                if api_password:
                    cfg["password"] = api_password
            self.client = getattr(ccxt_async, exchange_id)(cfg)

    async def _call(self, label: str, fn, *args, retries: int = 3, **kwargs):  # type: ignore[no-untyped-def]
        async def run():  # type: ignore[no-untyped-def]
            try:
                return await fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                raise _map_ccxt_error(exc) from None

        return await with_retries(run, attempts=retries, label=f"{self.name}.{label}")

    # ------------------------------------------------------------------ lifecycle
    async def connect(self) -> None:
        markets = await self._call("load_markets", self.client.load_markets)
        import ccxt

        tick_mode = getattr(self.client, "precisionMode", None) == ccxt.TICK_SIZE
        for symbol, m in markets.items():
            if not m.get("spot"):
                continue
            prec = m.get("precision") or {}
            amount_p, price_p = prec.get("amount"), prec.get("price")

            def step(p: Any) -> float:
                if p is None:
                    return 1e-8
                return float(p) if tick_mode else 10 ** (-float(p))

            limits = m.get("limits") or {}
            self._markets[symbol] = MarketInfo(
                symbol=symbol, base=m["base"], quote=m["quote"], asset_class=AssetClass.CRYPTO,
                qty_step=step(amount_p), price_step=step(price_p),
                min_qty=float((limits.get("amount") or {}).get("min") or 0.0),
                min_notional=float((limits.get("cost") or {}).get("min") or 0.0),
                maker_fee=float(m.get("maker") or 0.001), taker_fee=float(m.get("taker") or 0.001),
                fractional=True, active=bool(m.get("active", True)),
            )

    async def close(self) -> None:
        try:
            await self.client.close()
        except Exception:  # noqa: BLE001
            pass

    def market_info(self, symbol: str) -> MarketInfo:
        if symbol in self._markets:
            return self._markets[symbol]
        if symbol in self._fallback:
            return self._fallback[symbol]
        raise OrderRejected(f"{symbol} is not a spot market on {self.name}")

    def supports(self, symbol: str) -> bool:
        return symbol in self._markets

    # ------------------------------------------------------------------ market data
    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        rows = await self._call("fetch_ohlcv", self.client.fetch_ohlcv, symbol, timeframe, None, limit)
        return [
            Candle(datetime.fromtimestamp(r[0] / 1000, UTC), float(r[1]), float(r[2]), float(r[3]),
                   float(r[4]), float(r[5] or 0.0))
            for r in rows
        ]

    async def fetch_order_book(self, symbol: str, depth: int = 20) -> OrderBook:
        ob = await self._call("fetch_order_book", self.client.fetch_order_book, symbol, depth)
        ts_ms = ob.get("timestamp")
        ts = datetime.fromtimestamp(ts_ms / 1000, UTC) if ts_ms else datetime.now(UTC)
        return OrderBook(
            symbol,
            [(float(p), float(a)) for p, a, *_ in ob.get("bids", [])],
            [(float(p), float(a)) for p, a, *_ in ob.get("asks", [])],
            ts,
            venue=self.name,
        )

    async def fetch_ticker(self, symbol: str) -> Ticker:
        t = await self._call("fetch_ticker", self.client.fetch_ticker, symbol)
        ts = datetime.fromtimestamp(t["timestamp"] / 1000, UTC) if t.get("timestamp") else datetime.now(UTC)
        last = t.get("last") or t.get("close") or 0.0
        return Ticker(symbol, t.get("bid"), t.get("ask"), float(last), ts, self.name)

    # ------------------------------------------------------------------ account / trading
    def _require_keys(self) -> None:
        if not self.has_credentials:
            raise NotConfigured(
                f"{self.name}: API key/secret not configured (CRYPTO_API_KEY / CRYPTO_API_SECRET)"
            )

    async def fetch_balances(self) -> dict[str, Balance]:
        self._require_keys()
        raw = await self._call("fetch_balance", self.client.fetch_balance)
        out: dict[str, Balance] = {}
        for asset, free in (raw.get("free") or {}).items():
            used = (raw.get("used") or {}).get(asset) or 0.0
            if free or used:
                out[asset] = Balance(asset, float(free or 0.0), float(used))
        return out

    def _to_order(self, o: dict[str, Any]) -> ExchangeOrder:
        status = _CCXT_STATUS.get(str(o.get("status")), OrderStatus.OPEN)
        filled = float(o.get("filled") or 0.0)
        amount = float(o.get("amount") or 0.0)
        if status == OrderStatus.OPEN and filled > 0:
            status = OrderStatus.PARTIALLY_FILLED
        fee_cost, fee_ccy = 0.0, None
        fees = o.get("fees") or ([o["fee"]] if o.get("fee") else [])
        for f in fees:
            if f and f.get("cost") is not None:
                fee_cost += float(f["cost"])
                fee_ccy = f.get("currency") or fee_ccy
        ts = datetime.fromtimestamp(o["timestamp"] / 1000, UTC) if o.get("timestamp") else datetime.now(UTC)
        return ExchangeOrder(
            client_order_id=o.get("clientOrderId"), exchange_order_id=str(o.get("id")) if o.get("id") else None,
            symbol=o.get("symbol", ""), side=Side(o.get("side", "buy")),
            order_type=OrderType.LIMIT if o.get("type") == "limit" else OrderType.MARKET,
            status=status, quantity=amount, filled=filled,
            average_price=float(o["average"]) if o.get("average") else None,
            price=float(o["price"]) if o.get("price") else None, fee=fee_cost, fee_currency=fee_ccy,
            ts=ts, raw_status=str(o.get("status")),
        )

    async def create_order(self, req: OrderRequest) -> ExchangeOrder:
        self._require_keys()
        params = {"clientOrderId": req.client_order_id}
        # retries=1: order creation is retried by the execution engine only after a lookup by
        # client order id, never blindly (prevents duplicate orders after a timeout).
        o = await self._call(
            "create_order", self.client.create_order, req.symbol, req.order_type.value, req.side.value,
            req.quantity, req.price, params, retries=1,
        )
        result = self._to_order(o)
        result.client_order_id = result.client_order_id or req.client_order_id
        return result

    async def cancel_order(self, symbol: str, exchange_order_id: str | None, client_order_id: str | None) -> ExchangeOrder:
        self._require_keys()
        if not exchange_order_id:
            found = await self.fetch_order(symbol, None, client_order_id)
            if found is None or not found.exchange_order_id:
                raise OrderNotFound(f"order {client_order_id} not found on {self.name}")
            exchange_order_id = found.exchange_order_id
        await self._call("cancel_order", self.client.cancel_order, exchange_order_id, symbol)
        refreshed = await self.fetch_order(symbol, exchange_order_id, client_order_id)
        if refreshed is None:
            raise OrderNotFound(exchange_order_id)
        return refreshed

    async def fetch_order(self, symbol: str, exchange_order_id: str | None, client_order_id: str | None) -> ExchangeOrder | None:
        self._require_keys()
        if exchange_order_id:
            try:
                o = await self._call("fetch_order", self.client.fetch_order, exchange_order_id, symbol)
                return self._to_order(o)
            except OrderNotFound:
                return None
        # Lookup by client order id: scan open then recent orders.
        for method in ("fetch_open_orders", "fetch_closed_orders", "fetch_canceled_orders"):
            fn = getattr(self.client, method, None)
            if fn is None or not (getattr(self.client, "has", {}) or {}).get(_has_key(method), False):
                continue
            try:
                rows = await self._call(method, fn, symbol)
            except (ExchangeError, NotImplementedError):
                continue
            for o in rows:
                if o.get("clientOrderId") == client_order_id:
                    return self._to_order(o)
        return None

    async def fetch_open_orders(self, symbol: str | None = None) -> list[ExchangeOrder]:
        self._require_keys()
        rows = await self._call("fetch_open_orders", self.client.fetch_open_orders, symbol)
        return [self._to_order(o) for o in rows]

    def capabilities(self) -> dict[str, Any]:
        return {"client_order_id": True, "fractional": True, "spot_only": True, "paper": False,
                "withdrawals": False}


def _has_key(method: str) -> str:
    """fetch_open_orders -> fetchOpenOrders (CCXT 'has' keys)."""
    head, *rest = method.split("_")
    return head + "".join(p.capitalize() for p in rest)
