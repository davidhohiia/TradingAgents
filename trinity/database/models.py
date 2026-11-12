"""Database row <-> domain model mapping and schema metadata."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from trinity.core.models import (
    AssetClass,
    Mode,
    OrderRecord,
    OrderStatus,
    OrderType,
    Position,
    Side,
    ensure_utc,
)

REQUIRED_TABLES = (
    "accounts",
    "strategies",
    "balances",
    "positions",
    "orders",
    "executions",
    "trades",
    "daily_metrics",
    "risk_events",
    "market_data",
    "bot_health",
    "system_events",
    "deposits",
    "capital_allocations",
    "reallocation_proposals",
    "api_health",
    "funding_payments",
)


def ts_to_str(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    return ensure_utc(ts).isoformat()


def str_to_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return ensure_utc(datetime.fromisoformat(value))


def row_to_order(row: sqlite3.Row) -> OrderRecord:
    return OrderRecord(
        client_order_id=row["client_order_id"],
        strategy_id=row["strategy_id"],
        venue=row["venue"],
        symbol=row["symbol"],
        mode=Mode(row["mode"]),
        side=Side(row["side"]),
        order_type=OrderType(row["order_type"]),
        quantity=float(row["quantity"]),
        status=OrderStatus(row["status"]),
        price=row["price"],
        stop_price=row["stop_price"],
        take_profit=row["take_profit"],
        reduce_only=bool(row["reduce_only"]),
        exchange_order_id=row["exchange_order_id"],
        filled_quantity=float(row["filled_quantity"]),
        avg_fill_price=row["avg_fill_price"],
        fee_usd=float(row["fee_usd"]),
        reserved_usd=float(row["reserved_usd"]),
        reason=row["reason"] or "",
        parent_client_order_id=row["parent_client_order_id"],
        error=row["error"],
        created_at=str_to_ts(row["created_at"]),  # type: ignore[arg-type]
        updated_at=str_to_ts(row["updated_at"]),  # type: ignore[arg-type]
        sub_strategy=row["sub_strategy"] or "",
        leverage=float(row["leverage"] or 1.0),
    )


def row_to_position(row: sqlite3.Row) -> Position:
    return Position(
        strategy_id=row["strategy_id"],
        venue=row["venue"],
        symbol=row["symbol"],
        mode=Mode(row["mode"]),
        quantity=float(row["quantity"]),
        avg_entry_price=float(row["avg_entry_price"]),
        realized_pnl=float(row["realized_pnl"]),
        fees_paid=float(row["fees_paid"]),
        last_price=row["last_price"],
        stop_price=row["stop_price"],
        take_profit=row["take_profit"],
        opened_at=str_to_ts(row["opened_at"]),
        updated_at=str_to_ts(row["updated_at"]),  # type: ignore[arg-type]
        correlation_group=row["correlation_group"] or "",
        asset_class=AssetClass(row["asset_class"]),
        sub_strategy=row["sub_strategy"] or "",
        leverage=float(row["leverage"] or 1.0),
        liquidation_price=row["liquidation_price"],
        funding_paid=float(row["funding_paid"] or 0.0),
    )
