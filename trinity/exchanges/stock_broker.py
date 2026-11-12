"""Stock broker abstraction + Alpaca adapter.

Do not assume every instrument is available at every broker. Before a broker is used
the ``verify_capabilities`` check confirms: API reachable, trading allowed, instrument
tradable, fractional support, fee model, account restrictions (cash account, no margin).

Alpaca endpoints follow the official documentation (https://docs.alpaca.markets):
  trading  https://paper-api.alpaca.markets/v2/...  (paper)  |  https://api.alpaca.markets/v2/... (live)
  data     https://data.alpaca.markets/v2/stocks/{symbol}/bars , /quotes/latest
They have NOT been exercised against a real account from this development environment
(no credentials, outbound network to brokers blocked). Verify with your own paper account first.
"""

from __future__ import annotations

import csv
from abc import abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

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
    MarketDataSource,
    NotConfigured,
    OrderNotFound,
    OrderRejected,
    RateLimited,
    with_retries,
)

log = get_logger(__name__)


@dataclass
class BrokerCapabilityReport:
    broker: str
    api_reachable: bool = False
    trading_allowed: bool = False
    cash_account: bool = False
    fractional_supported: bool = False
    instruments: dict[str, dict[str, bool]] = field(default_factory=dict)
    fees: str = ""
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.api_reachable and self.trading_allowed and self.cash_account and not self.problems


class StockBroker(BaseExchange):
    """Abstract stock broker. Concrete brokers must implement capability verification."""

    asset_class = AssetClass.STOCK

    @abstractmethod
    async def verify_capabilities(self, symbols: list[str]) -> BrokerCapabilityReport: ...

    @abstractmethod
    async def fetch_positions(self) -> dict[str, tuple[float, float]]:
        """symbol -> (quantity, avg_entry_price)"""


# ---------------------------------------------------------------------------- Alpaca
ALPACA_PAPER_URL = "https://paper-api.alpaca.markets"
ALPACA_LIVE_URL = "https://api.alpaca.markets"
ALPACA_DATA_URL = "https://data.alpaca.markets"

_ALPACA_STATUS = {
    "new": OrderStatus.OPEN,
    "accepted": OrderStatus.OPEN,
    "pending_new": OrderStatus.OPEN,
    "accepted_for_bidding": OrderStatus.OPEN,
    "held": OrderStatus.OPEN,
    "calculated": OrderStatus.OPEN,
    "pending_cancel": OrderStatus.OPEN,
    "pending_replace": OrderStatus.OPEN,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "done_for_day": OrderStatus.EXPIRED,
    "canceled": OrderStatus.CANCELED,
    "replaced": OrderStatus.CANCELED,
    "expired": OrderStatus.EXPIRED,
    "rejected": OrderStatus.REJECTED,
    "stopped": OrderStatus.OPEN,
    "suspended": OrderStatus.OPEN,
}


class _AlpacaHttp:
    def __init__(self, key_id: str, secret: str, timeout: float = 15.0, transport: Any = None) -> None:
        self.headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret}
        self.client = httpx.AsyncClient(timeout=timeout, headers=self.headers, transport=transport)

    async def request(self, method: str, url: str, **kwargs: Any) -> Any:
        async def run() -> Any:
            try:
                r = await self.client.request(method, url, **kwargs)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise ExchangeUnavailable(f"alpaca network error: {type(exc).__name__}") from None
            if r.status_code == 429:
                raise RateLimited("alpaca rate limit")
            if r.status_code in (401, 403):
                raise AuthError(f"alpaca auth/permission error {r.status_code}")
            if r.status_code == 404:
                raise OrderNotFound(f"alpaca 404 for {url.split('/v2/')[-1]}")
            if r.status_code >= 500:
                raise ExchangeUnavailable(f"alpaca server error {r.status_code}")
            if r.status_code == 422 or r.status_code == 400:
                msg = r.text[:300]
                if "insufficient" in msg.lower():
                    raise InsufficientFunds(msg)
                raise OrderRejected(msg)
            if r.status_code == 204 or not r.content:
                return None
            return r.json()

        retries = 1 if method == "POST" else 3
        return await with_retries(run, attempts=retries, label=f"alpaca {method}")

    async def close(self) -> None:
        await self.client.aclose()


class AlpacaDataProvider(MarketDataSource):
    """Market data from the Alpaca Market Data API v2 (requires Alpaca keys; free IEX feed)."""

    def __init__(self, key_id: str, secret: str, feed: str = "iex", transport: Any = None) -> None:
        self.name = "alpaca-data"
        self.asset_class = AssetClass.STOCK
        self.feed = feed
        self.http = _AlpacaHttp(key_id, secret, transport=transport)

    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        days = int(limit * 1.6) + 10 if timeframe == "1Day" else 30
        start = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        params: dict[str, Any] = {"timeframe": timeframe, "start": start, "limit": 10000,
                                  "adjustment": "all", "feed": self.feed}
        bars: list[dict[str, Any]] = []
        token = None
        for _ in range(10):
            if token:
                params["page_token"] = token
            data = await self.http.request("GET", f"{ALPACA_DATA_URL}/v2/stocks/{symbol}/bars", params=params)
            bars.extend(data.get("bars") or [])
            token = data.get("next_page_token")
            if not token:
                break
        candles = [
            Candle(datetime.fromisoformat(b["t"].replace("Z", "+00:00")), float(b["o"]), float(b["h"]),
                   float(b["l"]), float(b["c"]), float(b["v"]))
            for b in bars
        ]
        return candles[-limit:]

    async def _quote(self, symbol: str) -> tuple[float, float, float, float, datetime]:
        data = await self.http.request(
            "GET", f"{ALPACA_DATA_URL}/v2/stocks/{symbol}/quotes/latest", params={"feed": self.feed}
        )
        q = data.get("quote") or {}
        bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
        if bid <= 0 or ask <= 0:
            raise ExchangeUnavailable(f"no quote for {symbol}")
        ts = _parse_rfc3339(q["t"]) if q.get("t") else datetime.now(UTC)
        return bid, ask, float(q.get("bs") or 1), float(q.get("as") or 1), ts

    async def fetch_ticker(self, symbol: str) -> Ticker:
        bid, ask, _, _, ts = await self._quote(symbol)
        return Ticker(symbol, bid, ask, (bid + ask) / 2, ts, self.name)

    async def fetch_order_book(self, symbol: str, depth: int = 20) -> OrderBook:
        bid, ask, bid_sz, ask_sz, ts = await self._quote(symbol)
        # Top-of-book only (sizes are in round lots of 100 shares on the IEX feed): synthetic=True
        return OrderBook(symbol, [(bid, bid_sz * 100)], [(ask, ask_sz * 100)], ts, venue=self.name, synthetic=True)

    async def close(self) -> None:
        await self.http.close()


class CsvStockData(MarketDataSource):
    """Daily bars from CSV files (data/csv/<SYMBOL>.csv with ts,open,high,low,close,volume).

    Intended for backtests. Quotes are derived from the last close with a configured
    spread and flagged synthetic.
    """

    def __init__(self, directory: Path, spread_bps: float = 2.0) -> None:
        self.name = "csv"
        self.asset_class = AssetClass.STOCK
        self.dir = directory
        self.spread = spread_bps / 10_000

    def load(self, symbol: str) -> list[Candle]:
        path = self.dir / f"{symbol.replace('/', '_')}.csv"
        if not path.exists():
            raise NotConfigured(f"no CSV data file {path}")
        out = []
        with open(path, encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                ts = datetime.fromisoformat(row["ts"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                out.append(Candle(ts, float(row["open"]), float(row["high"]), float(row["low"]),
                                  float(row["close"]), float(row["volume"])))
        return sorted(out, key=lambda c: c.ts)

    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        return self.load(symbol)[-limit:]

    async def fetch_ticker(self, symbol: str) -> Ticker:
        last = self.load(symbol)[-1]
        return Ticker(symbol, last.close * (1 - self.spread / 2), last.close * (1 + self.spread / 2),
                      last.close, last.ts, self.name)

    async def fetch_order_book(self, symbol: str, depth: int = 20) -> OrderBook:
        t = await self.fetch_ticker(symbol)
        return OrderBook(symbol, [(t.bid or 0, 1e9)], [(t.ask or 0, 1e9)], t.ts, self.name, synthetic=True)


class AlpacaBroker(StockBroker):
    def __init__(
        self,
        key_id: str | None,
        secret: str | None,
        *,
        paper_account: bool = True,
        instruments_fallback: dict[str, MarketInfo] | None = None,
        data: MarketDataSource | None = None,
        transport: Any = None,
    ) -> None:
        if not key_id or not secret:
            raise NotConfigured("Alpaca keys missing: set ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY")
        self.name = "alpaca"
        self.base = ALPACA_PAPER_URL if paper_account else ALPACA_LIVE_URL
        self.http = _AlpacaHttp(key_id, secret, transport=transport)
        self.data = data or AlpacaDataProvider(key_id, secret, transport=transport)
        self._fallback = instruments_fallback or {}
        self._assets: dict[str, dict[str, Any]] = {}

    async def connect(self) -> None:
        await self.http.request("GET", f"{self.base}/v2/account")

    async def close(self) -> None:
        await self.http.close()
        close = getattr(self.data, "close", None)
        if close:
            await close()

    async def verify_capabilities(self, symbols: list[str]) -> BrokerCapabilityReport:
        rep = BrokerCapabilityReport(broker="alpaca", fees="commission-free US equities; SEC/FINRA sell fees apply (verify)")
        try:
            acct = await self.http.request("GET", f"{self.base}/v2/account")
            rep.api_reachable = True
        except ExchangeError as exc:
            rep.problems.append(f"account endpoint failed: {exc}")
            return rep
        rep.trading_allowed = not acct.get("trading_blocked") and not acct.get("account_blocked") \
            and str(acct.get("status", "")).upper() == "ACTIVE"
        if not rep.trading_allowed:
            rep.problems.append("account trading blocked or not ACTIVE")
        multiplier = float(acct.get("multiplier") or 1)
        rep.cash_account = multiplier <= 1.0
        if not rep.cash_account:
            # A margin account is acceptable ONLY because TRINITY never uses more than cash;
            # still flagged so the user can request a cash account / disable margin.
            rep.problems.append(f"account multiplier {multiplier} > 1 (margin account): use a cash account")
        for sym in symbols:
            try:
                a = await self.http.request("GET", f"{self.base}/v2/assets/{sym}")
                self._assets[sym] = a
                rep.instruments[sym] = {"tradable": bool(a.get("tradable")),
                                        "fractionable": bool(a.get("fractionable")),
                                        "active": a.get("status") == "active"}
                if not a.get("tradable"):
                    rep.problems.append(f"{sym} not tradable at Alpaca")
            except OrderNotFound:
                rep.instruments[sym] = {"tradable": False, "fractionable": False, "active": False}
                rep.problems.append(f"{sym} unknown at Alpaca")
        rep.fractional_supported = any(v["fractionable"] for v in rep.instruments.values())
        return rep

    def market_info(self, symbol: str) -> MarketInfo:
        fb = self._fallback.get(symbol)
        if fb is None:
            raise OrderRejected(f"{symbol} not configured")
        asset = self._assets.get(symbol)
        fractional = bool(asset.get("fractionable")) if asset else False
        if fractional:
            return fb
        return MarketInfo(fb.symbol, fb.base, fb.quote, fb.asset_class, 1.0, fb.price_step, 1.0,
                          fb.min_notional, fb.maker_fee, fb.taker_fee, fractional=False,
                          active=bool(asset.get("tradable")) if asset else False)

    async def is_market_open(self, now: datetime) -> bool:  # noqa: ARG002
        clock = await self.http.request("GET", f"{self.base}/v2/clock")
        return bool(clock.get("is_open"))

    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        return await self.data.fetch_ohlcv(symbol, timeframe, limit)

    async def fetch_order_book(self, symbol: str, depth: int = 20) -> OrderBook:
        return await self.data.fetch_order_book(symbol, depth)

    async def fetch_ticker(self, symbol: str) -> Ticker:
        return await self.data.fetch_ticker(symbol)

    async def fetch_balances(self) -> dict[str, Balance]:
        acct = await self.http.request("GET", f"{self.base}/v2/account")
        ccy = acct.get("currency", "USD")
        # Use CASH, never buying_power (buying power may include margin).
        return {ccy: Balance(ccy, float(acct.get("cash") or 0.0), 0.0)}

    async def fetch_positions(self) -> dict[str, tuple[float, float]]:
        rows = await self.http.request("GET", f"{self.base}/v2/positions")
        return {r["symbol"]: (float(r["qty"]), float(r["avg_entry_price"])) for r in rows or []}

    def _to_order(self, o: dict[str, Any]) -> ExchangeOrder:
        filled = float(o.get("filled_qty") or 0)
        status = _ALPACA_STATUS.get(o.get("status", ""), OrderStatus.OPEN)
        qty = float(o.get("qty") or 0) or filled
        return ExchangeOrder(
            client_order_id=o.get("client_order_id"), exchange_order_id=o.get("id"), symbol=o.get("symbol", ""),
            side=Side(o.get("side", "buy")),
            order_type=OrderType.LIMIT if o.get("type") == "limit" else OrderType.MARKET,
            status=status, quantity=qty, filled=filled,
            average_price=float(o["filled_avg_price"]) if o.get("filled_avg_price") else None,
            price=float(o["limit_price"]) if o.get("limit_price") else None, fee=0.0, fee_currency="USD",
            raw_status=o.get("status", ""),
        )

    async def create_order(self, req: OrderRequest) -> ExchangeOrder:
        body: dict[str, Any] = {
            "symbol": req.symbol, "qty": f"{req.quantity:.6f}".rstrip("0").rstrip("."),
            "side": req.side.value, "type": req.order_type.value, "time_in_force": "day",
            "client_order_id": req.client_order_id,
        }
        if req.order_type == OrderType.LIMIT:
            body["limit_price"] = f"{req.price:.2f}"
        o = await self.http.request("POST", f"{self.base}/v2/orders", json=body)
        return self._to_order(o)

    async def fetch_order(self, symbol: str, exchange_order_id: str | None, client_order_id: str | None) -> ExchangeOrder | None:
        try:
            if exchange_order_id:
                o = await self.http.request("GET", f"{self.base}/v2/orders/{exchange_order_id}")
            else:
                o = await self.http.request("GET", f"{self.base}/v2/orders:by_client_order_id",
                                            params={"client_order_id": client_order_id})
        except OrderNotFound:
            return None
        return self._to_order(o)

    async def cancel_order(self, symbol: str, exchange_order_id: str | None, client_order_id: str | None) -> ExchangeOrder:
        if not exchange_order_id:
            found = await self.fetch_order(symbol, None, client_order_id)
            if found is None:
                raise OrderNotFound(str(client_order_id))
            exchange_order_id = found.exchange_order_id
        await self.http.request("DELETE", f"{self.base}/v2/orders/{exchange_order_id}")
        refreshed = await self.fetch_order(symbol, exchange_order_id, None)
        if refreshed is None:
            raise OrderNotFound(str(exchange_order_id))
        return refreshed

    async def fetch_open_orders(self, symbol: str | None = None) -> list[ExchangeOrder]:
        params: dict[str, Any] = {"status": "open", "limit": 500}
        if symbol:
            params["symbols"] = symbol
        rows = await self.http.request("GET", f"{self.base}/v2/orders", params=params)
        return [self._to_order(o) for o in rows or []]

    def capabilities(self) -> dict[str, Any]:
        return {"client_order_id": True, "fractional": "per-asset", "spot_only": True, "paper": False}


def _parse_rfc3339(value: str) -> datetime:
    """Parse RFC3339 timestamps with nanoseconds (e.g. 2024-01-02T15:04:05.123456789Z)."""
    v = value.replace("Z", "+00:00")
    if "." in v:
        head, rest = v.split(".", 1)
        frac, tz = (rest.split("+", 1) + [""])[:2] if "+" in rest else (rest, "00:00")
        v = f"{head}.{frac[:6]}+{tz or '00:00'}"
    return datetime.fromisoformat(v).astimezone(UTC)
