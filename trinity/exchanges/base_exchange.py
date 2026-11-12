"""Exchange / broker abstraction.

Bots receive only read-only market data access (``MarketDataSource``).
Trading methods (create/cancel order) are used exclusively by the execution engine.
"""

from __future__ import annotations

import asyncio
import random
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, TypeVar

from trinity.core.log import get_logger
from trinity.core.models import (
    AssetClass,
    Balance,
    Candle,
    ExchangeOrder,
    MarketInfo,
    OrderBook,
    OrderRequest,
    Ticker,
)

log = get_logger(__name__)
T = TypeVar("T")


class ExchangeError(Exception):
    """Base class for venue errors."""


class ExchangeUnavailable(ExchangeError):
    """Network problem, timeout, maintenance. Retryable."""


class RateLimited(ExchangeUnavailable):
    pass


class AuthError(ExchangeError):
    """Invalid/missing credentials or insufficient API permissions. NOT retryable."""


class OrderRejected(ExchangeError):
    """Venue rejected the order (invalid params, min size, duplicate id...)."""


class InsufficientFunds(OrderRejected):
    pass


class OrderNotFound(ExchangeError):
    pass


class NotConfigured(ExchangeError):
    """Adapter exists but credentials / provider are missing."""


class MarketDataSource(ABC):
    """Read-only market data interface (safe to hand to bots)."""

    name: str
    asset_class: AssetClass

    @abstractmethod
    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[Candle]: ...

    @abstractmethod
    async def fetch_order_book(self, symbol: str, depth: int = 20) -> OrderBook: ...

    @abstractmethod
    async def fetch_ticker(self, symbol: str) -> Ticker: ...

    async def is_market_open(self, now: datetime) -> bool:  # noqa: ARG002
        return True


class BaseExchange(MarketDataSource):
    """Trading-capable venue adapter. Only the execution engine may call trading methods."""

    is_paper: bool = False

    async def connect(self) -> None:  # noqa: B027
        """Load markets / verify connectivity."""

    async def close(self) -> None:  # noqa: B027
        """Release network resources."""

    @abstractmethod
    def market_info(self, symbol: str) -> MarketInfo: ...

    @abstractmethod
    async def fetch_balances(self) -> dict[str, Balance]: ...

    @abstractmethod
    async def create_order(self, req: OrderRequest) -> ExchangeOrder: ...

    @abstractmethod
    async def cancel_order(
        self, symbol: str, exchange_order_id: str | None, client_order_id: str | None
    ) -> ExchangeOrder: ...

    @abstractmethod
    async def fetch_order(
        self, symbol: str, exchange_order_id: str | None, client_order_id: str | None
    ) -> ExchangeOrder | None: ...

    @abstractmethod
    async def fetch_open_orders(self, symbol: str | None = None) -> list[ExchangeOrder]: ...

    def capabilities(self) -> dict[str, Any]:
        return {"client_order_id": True, "fractional": True, "spot_only": True}


class ReadOnlyMarketData(MarketDataSource):
    """Wraps a venue and exposes ONLY market-data methods to bots."""

    def __init__(self, inner: MarketDataSource) -> None:
        self._inner = inner
        self.name = inner.name
        self.asset_class = inner.asset_class

    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        return await self._inner.fetch_ohlcv(symbol, timeframe, limit)

    async def fetch_order_book(self, symbol: str, depth: int = 20) -> OrderBook:
        return await self._inner.fetch_order_book(symbol, depth)

    async def fetch_ticker(self, symbol: str) -> Ticker:
        return await self._inner.fetch_ticker(symbol)

    async def is_market_open(self, now: datetime) -> bool:
        return await self._inner.is_market_open(now)

    def market_info(self, symbol: str) -> MarketInfo:
        fn = getattr(self._inner, "market_info", None)
        if fn is None:
            raise AttributeError("market_info not available")
        return fn(symbol)


async def with_retries(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 10.0,
    retry_on: tuple[type[BaseException], ...] = (ExchangeUnavailable,),
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    label: str = "",
) -> T:
    """Retry an idempotent call with exponential backoff + jitter.

    NEVER use this around order creation without a client order id (non-idempotent).
    """
    last: BaseException | None = None
    for i in range(attempts):
        try:
            return await fn()
        except retry_on as exc:
            last = exc
            if i == attempts - 1:
                break
            delay = min(max_delay, base_delay * (2**i)) * (0.5 + random.random() / 2)
            if isinstance(exc, RateLimited):
                delay = max(delay, 2.0)
            log.warning("retryable error in %s (attempt %d/%d): %s", label, i + 1, attempts, exc)
            await sleep(delay)
    assert last is not None
    raise last
