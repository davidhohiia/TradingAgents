"""Domain models shared by all TRINITY modules.

Business logic only — no exchange or database specifics here.
All timestamps are timezone-aware UTC datetimes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


class Mode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class AssetClass(StrEnum):
    CRYPTO = "crypto"
    STOCK = "stock"


class OrderStatus(StrEnum):
    PENDING_SUBMIT = "pending_submit"  # persisted locally, not yet acknowledged by venue
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    FAILED = "failed"  # never reached the venue / unknown after reconciliation

    @property
    def is_active(self) -> bool:
        return self in ACTIVE_ORDER_STATUSES

    @property
    def is_final(self) -> bool:
        return not self.is_active


ACTIVE_ORDER_STATUSES = frozenset(
    {OrderStatus.PENDING_SUBMIT, OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED}
)


class BotStatus(StrEnum):
    PAPER_MODE = "PAPER MODE"
    LIVE_MODE = "LIVE MODE"
    STOPPED = "STOPPED"
    PAUSED = "PAUSED"
    RISK_LOCKED = "RISK LOCKED"
    CONNECTION_ERROR = "CONNECTION ERROR"
    SCAN_ONLY = "SCAN ONLY (VIRTUAL)"
    NO_DATA = "NO DATA SOURCE"


class MarketRegime(StrEnum):
    RANGING = "ranging"
    BULL = "bull"
    BEAR = "bear"
    STRONG_BEAR = "strong_bear"
    HIGH_VOLATILITY = "high_volatility"
    UNKNOWN = "unknown"


def new_client_order_id(strategy_code: str) -> str:
    """Unique client order id: <=32 chars, [A-Za-z0-9-] (Binance/OKX/Alpaca safe)."""
    code = "".join(ch for ch in strategy_code.lower() if ch.isalnum())[:4] or "x"
    return f"tr{code}-{uuid.uuid4().hex[:24]}"


@dataclass(frozen=True)
class Candle:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class OrderBook:
    symbol: str
    bids: list[tuple[float, float]]  # (price, amount) best first
    asks: list[tuple[float, float]]
    ts: datetime
    venue: str = ""
    synthetic: bool = False  # True when built from quotes, not a real depth snapshot

    @property
    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread_pct(self) -> float | None:
        mid = self.mid
        if not mid or self.best_ask is None or self.best_bid is None:
            return None
        return (self.best_ask - self.best_bid) / mid


@dataclass(frozen=True)
class Ticker:
    symbol: str
    bid: float | None
    ask: float | None
    last: float
    ts: datetime
    venue: str = ""


@dataclass(frozen=True)
class MarketInfo:
    symbol: str
    base: str
    quote: str
    asset_class: AssetClass
    qty_step: float
    price_step: float
    min_qty: float
    min_notional: float
    maker_fee: float
    taker_fee: float
    fractional: bool = True
    active: bool = True


@dataclass
class Balance:
    asset: str
    free: float
    used: float

    @property
    def total(self) -> float:
        return self.free + self.used


@dataclass
class OrderIntent:
    """A request from a bot. It is NOT an order until the execution engine accepts it."""

    strategy_id: str
    venue: str
    symbol: str
    side: Side
    order_type: OrderType
    quantity: float
    reference_price: float
    market_data_ts: datetime
    asset_class: AssetClass
    price: float | None = None  # limit price
    stop_price: float | None = None  # protective stop used for risk sizing
    take_profit: float | None = None
    reduce_only: bool = False
    protective_exit: bool = False  # stop-loss style exit (allowed while risk locked)
    reason: str = ""
    parent_client_order_id: str | None = None
    client_order_id: str = ""
    sub_strategy: str = ""  # e.g. "momentum" / "breakout" (separate statistics)
    leverage: float = 1.0  # 1.0 = spot. >1 only via the LeverageGuard (disabled by default)
    quality_score: float | None = None  # Trade Quality Score 0..100 (required for entries)
    tags: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.client_order_id:
            self.client_order_id = new_client_order_id(self.strategy_id)
        self.market_data_ts = ensure_utc(self.market_data_ts)

    @property
    def effective_price(self) -> float:
        return self.price if self.price is not None else self.reference_price

    @property
    def notional(self) -> float:
        return abs(self.quantity) * self.effective_price


@dataclass
class OrderRequest:
    """What the execution engine sends to an exchange adapter."""

    client_order_id: str
    symbol: str
    side: Side
    order_type: OrderType
    quantity: float
    price: float | None = None


@dataclass
class ExchangeOrder:
    """Normalized order state as reported by a venue."""

    client_order_id: str | None
    exchange_order_id: str | None
    symbol: str
    side: Side
    order_type: OrderType
    status: OrderStatus
    quantity: float
    filled: float
    average_price: float | None
    price: float | None = None
    fee: float = 0.0
    fee_currency: str | None = None
    ts: datetime = field(default_factory=utcnow)
    raw_status: str = ""
    is_maker: bool | None = None


@dataclass
class OrderRecord:
    """Order as stored in the local database."""

    client_order_id: str
    strategy_id: str
    venue: str
    symbol: str
    mode: Mode
    side: Side
    order_type: OrderType
    quantity: float
    status: OrderStatus
    price: float | None = None
    stop_price: float | None = None
    take_profit: float | None = None
    reduce_only: bool = False
    exchange_order_id: str | None = None
    filled_quantity: float = 0.0
    avg_fill_price: float | None = None
    fee_usd: float = 0.0
    reserved_usd: float = 0.0
    reason: str = ""
    parent_client_order_id: str | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    sub_strategy: str = ""
    leverage: float = 1.0
    quality_score: float | None = None

    @property
    def remaining(self) -> float:
        return max(self.quantity - self.filled_quantity, 0.0)


@dataclass
class Fill:
    exec_id: str
    client_order_id: str
    strategy_id: str
    venue: str
    symbol: str
    mode: Mode
    side: Side
    quantity: float
    price: float
    fee_usd: float
    ts: datetime
    fee: float = 0.0
    fee_currency: str | None = None
    exchange_order_id: str | None = None
    is_maker: bool | None = None
    realized_pnl: float = 0.0


@dataclass
class Position:
    strategy_id: str
    venue: str
    symbol: str
    mode: Mode
    quantity: float = 0.0
    avg_entry_price: float = 0.0
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    last_price: float | None = None
    stop_price: float | None = None
    take_profit: float | None = None
    opened_at: datetime | None = None
    updated_at: datetime = field(default_factory=utcnow)
    correlation_group: str = ""
    asset_class: AssetClass = AssetClass.CRYPTO
    sub_strategy: str = ""
    leverage: float = 1.0
    liquidation_price: float | None = None
    funding_paid: float = 0.0
    cycle: dict[str, Any] | None = None  # open trade cycle (journal accumulator)

    @property
    def margin_used(self) -> float:
        """Capital tied up: full cost for spot, cost / leverage for isolated margin."""
        return self.cost_basis / max(self.leverage, 1.0)

    @property
    def risk_to_stop(self) -> float:
        """Capital lost if the protective stop is hit (vs entry). A stop at/above entry = 0 risk.
        Without a stop the whole cost basis is treated as at risk."""
        if not self.is_open:
            return 0.0
        if self.stop_price is None or self.stop_price <= 0:
            return self.cost_basis
        return max(self.avg_entry_price - self.stop_price, 0.0) * self.quantity

    @property
    def is_open(self) -> bool:
        return self.quantity > 1e-12

    @property
    def cost_basis(self) -> float:
        return self.quantity * self.avg_entry_price

    @property
    def market_value(self) -> float:
        price = self.last_price if self.last_price is not None else self.avg_entry_price
        return self.quantity * price

    @property
    def unrealized_pnl(self) -> float:
        if not self.is_open or self.last_price is None:
            return 0.0
        return (self.last_price - self.avg_entry_price) * self.quantity


@dataclass
class RiskDecision:
    approved: bool
    reasons: list[str] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)

    @classmethod
    def ok(cls, checks: dict[str, bool] | None = None) -> RiskDecision:
        return cls(True, [], checks or {})

    @classmethod
    def reject(cls, *reasons: str) -> RiskDecision:
        return cls(False, list(reasons), {})


@dataclass
class SubmitResult:
    accepted: bool
    client_order_id: str
    order: OrderRecord | None = None
    reasons: list[str] = field(default_factory=list)
    duplicate: bool = False
