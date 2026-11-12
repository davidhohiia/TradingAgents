"""Position accounting (long-only, average cost including fees)."""

from __future__ import annotations

from datetime import datetime

from trinity.config.settings import AppConfig
from trinity.core.log import get_logger
from trinity.core.models import Fill, Mode, Position, Side
from trinity.database.repository import Repository

log = get_logger(__name__)

EPS = 1e-12


class PositionError(RuntimeError):
    pass


class PositionManager:
    def __init__(self, repo: Repository, config: AppConfig, mode: Mode) -> None:
        self.repo = repo
        self.config = config
        self.mode = mode
        self._positions: dict[tuple[str, str, str], Position] = {}
        self.reload()

    def reload(self) -> None:
        self._positions = {
            (p.strategy_id, p.venue, p.symbol): p for p in self.repo.list_positions(self.mode)
        }

    def get(self, strategy_id: str, venue: str, symbol: str) -> Position:
        key = (strategy_id, venue, symbol)
        if key not in self._positions:
            inst = self.config.instruments.get(symbol)
            self._positions[key] = Position(
                strategy_id=strategy_id, venue=venue, symbol=symbol, mode=self.mode,
                correlation_group=self.config.correlation_group(symbol),
                asset_class=inst.asset_class if inst else Position.asset_class,
            )
        return self._positions[key]

    def all(self, open_only: bool = False) -> list[Position]:
        return [p for p in self._positions.values() if p.is_open or not open_only]

    def open_positions(self, strategy_id: str | None = None) -> list[Position]:
        return [p for p in self.all(True) if strategy_id is None or p.strategy_id == strategy_id]

    def apply_fill(self, fill: Fill, exit_reason: str = "") -> float:
        """Update the position for a fill. Returns realized PnL (net of fees) of this fill.

        Must be called inside the same DB transaction that stores the execution.
        """
        p = self.get(fill.strategy_id, fill.venue, fill.symbol)
        realized = 0.0
        if fill.side == Side.BUY:
            new_qty = p.quantity + fill.quantity
            cost = p.quantity * p.avg_entry_price + fill.quantity * fill.price + fill.fee_usd
            p.avg_entry_price = cost / new_qty
            if not p.is_open:
                p.opened_at = fill.ts
            p.quantity = new_qty
        else:
            if fill.quantity > p.quantity + 1e-9:
                # Should be impossible (risk manager forbids shorting). Never create a short.
                raise PositionError(
                    f"sell fill {fill.quantity} exceeds position {p.quantity} for {fill.strategy_id}/{fill.symbol}"
                )
            qty = min(fill.quantity, p.quantity)
            realized = (fill.price - p.avg_entry_price) * qty - fill.fee_usd
            entry_price = p.avg_entry_price
            entry_time = p.opened_at
            p.quantity -= qty
            if p.quantity <= 1e-10:
                p.quantity = 0.0
                p.avg_entry_price = 0.0
                p.opened_at = None
                p.stop_price = None
                p.take_profit = None
            p.realized_pnl += realized
            self.repo.upsert_trade_fill(
                strategy_id=fill.strategy_id, venue=fill.venue, symbol=fill.symbol, mode=fill.mode,
                quantity=qty, entry_price=entry_price, exit_price=fill.price, entry_time=entry_time,
                exit_time=fill.ts, fees_usd=fill.fee_usd, realized_pnl=realized, exit_reason=exit_reason,
                exit_client_order_id=fill.client_order_id, is_virtual=fill.mode == Mode.PAPER,
            )
        p.fees_paid += fill.fee_usd
        p.last_price = fill.price if p.last_price is None else p.last_price
        p.updated_at = fill.ts
        self.repo.upsert_position(p)
        return realized

    def set_protection(self, strategy_id: str, venue: str, symbol: str,
                       stop: float | None, take_profit: float | None) -> None:
        p = self.get(strategy_id, venue, symbol)
        p.stop_price = stop
        p.take_profit = take_profit
        self.repo.upsert_position(p)

    def mark_to_market(self, symbol: str, price: float, ts: datetime) -> None:
        for p in self._positions.values():
            if p.symbol == symbol:
                p.last_price = price
                p.updated_at = ts
                if p.is_open:
                    self.repo.upsert_position(p)

    def totals(self) -> dict[str, float]:
        realized = sum(p.realized_pnl for p in self._positions.values())
        unrealized = sum(p.unrealized_pnl for p in self._positions.values())
        invested = sum(p.cost_basis for p in self._positions.values() if p.is_open)
        market_value = sum(p.market_value for p in self._positions.values() if p.is_open)
        return {"realized": realized, "unrealized": unrealized, "invested": invested, "market_value": market_value}

    def realized_by_strategy(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for p in self._positions.values():
            out[p.strategy_id] = out.get(p.strategy_id, 0.0) + p.realized_pnl
        return out
