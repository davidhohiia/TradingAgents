"""Trade journal model: one record per FULLY CLOSED trade (position cycle).

A trade cycle starts with the first entry fill on a flat position and ends when the position
is flat again. All partial entry and exit fills of the cycle are merged into ONE trade:
    entry price = sum(qty_i x price_i) / sum(qty_i) over entry fills  (raw fill prices, fees excluded)
    exit price  = sum(qty_j x price_j) / sum(qty_j) over exit fills
    gross P&L   = LONG:  exit_value - entry_value      SHORT: entry_value - exit_value
    net P&L     = gross - trading fees (entry + exit) - funding
    P&L %       = net / capital employed x 100, capital employed = entry_value / leverage
Only values recorded from confirmed fills are used - nothing is estimated or invented.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

JOURNAL_TZ = "Europe/Amsterdam"

TRADES_HEADER = [
    "ID сделки", "Название бота", "Торговый инструмент", "Направление", "Цена покупки", "Цена продажи",
    "Дата покупки", "Время покупки", "Дата продажи", "Время продажи", "Количество актива", "Комиссия",
    "Прибыль/убыток (USD)", "Прибыль/убыток (%)", "Стратегия", "Статус сделки", "Причина закрытия",
    # extra columns (after the required 17)
    "Валовая прибыль (USD)", "Funding (USD)", "Задействованный капитал (USD)", "Плечо", "Режим",
    "Частичных входов", "Частичных выходов",
]


@dataclass(frozen=True)
class PnL:
    gross: float
    fees: float
    funding: float
    net: float
    capital_employed: float
    net_pct: float


def compute_trade_pnl(direction: str, entry_price: float, exit_price: float, quantity: float, fees: float,
                      funding: float = 0.0, leverage: float = 1.0) -> PnL:
    """P&L of one closed trade. LONG: (exit - entry) x qty; SHORT: (entry - exit) x qty; minus all costs."""
    if quantity <= 0 or entry_price <= 0:
        raise ValueError("quantity and entry price must be positive")
    direction = direction.upper()
    if direction == "LONG":
        gross = (exit_price - entry_price) * quantity
    elif direction == "SHORT":
        gross = (entry_price - exit_price) * quantity
    else:
        raise ValueError(f"unknown direction {direction}")
    net = gross - fees - funding
    capital = entry_price * quantity / max(leverage, 1.0)
    return PnL(gross, fees, funding, net, capital, net / capital * 100.0)


@dataclass
class TradeCycle:
    """Accumulator for one position cycle (persisted with the position)."""

    opened_at: str
    first_entry_order: str
    direction: str = "LONG"
    sub_strategy: str = ""
    leverage: float = 1.0
    entry_qty: float = 0.0
    entry_value: float = 0.0
    entry_fees: float = 0.0
    entry_fills: int = 0
    entry_orders: list[str] = field(default_factory=list)
    exit_qty: float = 0.0
    exit_value: float = 0.0
    exit_fees: float = 0.0
    exit_fills: int = 0
    exit_orders: list[str] = field(default_factory=list)
    funding: float = 0.0
    reasons: list[str] = field(default_factory=list)
    last_exit_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> TradeCycle | None:
        return cls(**d) if d else None

    def add_entry(self, qty: float, price: float, fee_usd: float, order_id: str) -> None:
        self.entry_qty += qty
        self.entry_value += qty * price
        self.entry_fees += fee_usd
        self.entry_fills += 1
        if order_id not in self.entry_orders:
            self.entry_orders.append(order_id)

    def add_exit(self, qty: float, price: float, fee_usd: float, order_id: str, reason: str, ts: datetime) -> None:
        self.exit_qty += qty
        self.exit_value += qty * price
        self.exit_fees += fee_usd
        self.exit_fills += 1
        if order_id not in self.exit_orders:
            self.exit_orders.append(order_id)
        if reason and reason not in self.reasons:
            self.reasons.append(reason)
        self.last_exit_at = ts.isoformat()


@dataclass(frozen=True)
class JournalTrade:
    trade_id: str
    mode: str
    strategy_id: str
    venue: str
    symbol: str
    direction: str
    sub_strategy: str
    quantity: float
    entry_price: float
    exit_price: float
    entry_time: str  # ISO UTC
    exit_time: str  # ISO UTC
    fees: float
    funding: float
    gross_pnl: float
    net_pnl: float
    net_pct: float
    capital_employed: float
    leverage: float
    status: str
    close_reason: str
    entry_fills: int
    exit_fills: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def make_trade_id(mode: str, strategy_id: str, symbol: str, opened_at: str, first_order: str) -> str:
    """Deterministic, unique and stable across restarts."""
    digest = hashlib.sha256(f"{mode}|{strategy_id}|{symbol}|{opened_at}|{first_order}".encode()).hexdigest()[:10]
    ts = datetime.fromisoformat(opened_at).strftime("%Y%m%d%H%M%S")
    return f"{'L' if mode == 'live' else 'P'}-{symbol.replace('/', '')}-{ts}-{digest}"


def close_cycle(cycle: TradeCycle, *, mode: str, strategy_id: str, venue: str, symbol: str) -> JournalTrade:
    if cycle.entry_qty <= 0 or cycle.exit_qty <= 0:
        raise ValueError("cycle without entry or exit fills cannot be journaled")
    entry_price = cycle.entry_value / cycle.entry_qty
    exit_price = cycle.exit_value / cycle.exit_qty
    qty = cycle.exit_qty
    # entry value of the quantity actually closed (fee-in-base can make entry_qty != exit_qty by dust)
    pnl = compute_trade_pnl(cycle.direction, entry_price, exit_price, qty, cycle.entry_fees + cycle.exit_fees,
                            cycle.funding, cycle.leverage)
    return JournalTrade(
        trade_id=make_trade_id(mode, strategy_id, symbol, cycle.opened_at, cycle.first_entry_order),
        mode=mode, strategy_id=strategy_id, venue=venue, symbol=symbol, direction=cycle.direction,
        sub_strategy=cycle.sub_strategy, quantity=qty, entry_price=entry_price, exit_price=exit_price,
        entry_time=cycle.opened_at, exit_time=cycle.last_exit_at or cycle.opened_at, fees=pnl.fees,
        funding=pnl.funding, gross_pnl=pnl.gross, net_pnl=pnl.net, net_pct=pnl.net_pct,
        capital_employed=pnl.capital_employed, leverage=cycle.leverage, status="CLOSED",
        close_reason="; ".join(cycle.reasons)[:500], entry_fills=cycle.entry_fills, exit_fills=cycle.exit_fills,
    )


def local_date_time(iso_utc: str, tz: str = JOURNAL_TZ) -> tuple[str, str]:
    dt = datetime.fromisoformat(iso_utc).astimezone(ZoneInfo(tz))
    return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M:%S")


BOT_NAMES = {"crypto_momentum": "Crypto Momentum AI", "stock_trend": "Stock Trend AI", "arbitrage": "Arbitrage AI"}


def trade_row(t: JournalTrade | dict[str, Any], tz: str = JOURNAL_TZ) -> list[Any]:
    d = t.to_dict() if isinstance(t, JournalTrade) else t
    ed, et = local_date_time(d["entry_time"], tz)
    xd, xt = local_date_time(d["exit_time"], tz)
    return [
        d["trade_id"], BOT_NAMES.get(d["strategy_id"], d["strategy_id"]), d["symbol"], d["direction"],
        d["entry_price"], d["exit_price"], ed, et, xd, xt, d["quantity"], d["fees"], d["net_pnl"], d["net_pct"],
        d["sub_strategy"] or "-", d["status"] if d["mode"] == "live" else f"{d['status']} (VIRTUAL)",
        d["close_reason"] or "-",
        d["gross_pnl"], d["funding"], d["capital_employed"], d["leverage"],
        "LIVE" if d["mode"] == "live" else "PAPER (virtual)", d["entry_fills"], d["exit_fills"],
    ]
