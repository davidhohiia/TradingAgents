"""Position accounting (long-only, average cost including fees)."""

from __future__ import annotations

from datetime import datetime

from trinity.config.settings import AppConfig
from trinity.core.log import get_logger
from trinity.core.models import Fill, Mode, Position, Side
from trinity.database.repository import Repository
from trinity.integrations.sheets.journal import TradeCycle, close_cycle

log = get_logger(__name__)
DUST_USD = 0.01  # a remainder worth less than 1 cent (fee-in-base rounding) closes the trade cycle

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

    def apply_fill(self, fill: Fill, exit_reason: str = "", sub_strategy: str = "", leverage: float = 1.0,
                   stop_price: float | None = None, take_profit: float | None = None,
                   unsellable_qty: float = 0.0) -> float:
        """Update the position for a fill. Returns realized PnL (net of fees) of this fill.

        Must be called inside the same DB transaction that stores the execution.
        """
        p = self.get(fill.strategy_id, fill.venue, fill.symbol)
        realized = 0.0
        if fill.side == Side.BUY:
            if p.cycle is None:  # first entry of a new trade cycle
                p.cycle = TradeCycle(opened_at=fill.ts.isoformat(), first_entry_order=fill.client_order_id,
                                     sub_strategy=sub_strategy, leverage=max(leverage, 1.0)).to_dict()
            cyc = TradeCycle.from_dict(p.cycle)
            assert cyc is not None
            cyc.add_entry(fill.quantity, fill.price, fill.fee_usd, fill.client_order_id)
            p.cycle = cyc.to_dict()
            new_qty = p.quantity + fill.quantity
            cost = p.quantity * p.avg_entry_price + fill.quantity * fill.price + fill.fee_usd
            p.avg_entry_price = cost / new_qty
            if not p.is_open:
                p.opened_at = fill.ts
                p.sub_strategy = sub_strategy
                p.leverage = max(leverage, 1.0)
            p.quantity = new_qty
            if stop_price:
                p.stop_price = stop_price
            if take_profit:
                p.take_profit = take_profit
            if p.leverage > 1.0:
                from trinity.risk.leverage import liquidation_price_long

                p.liquidation_price = liquidation_price_long(
                    p.avg_entry_price, p.leverage, self.config.leverage.maintenance_margin_rate)
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
                p.liquidation_price = None
            p.realized_pnl += realized
            self._journal_exit(p, fill, qty, exit_reason, unsellable_qty)
            self.repo.upsert_trade_fill(
                strategy_id=fill.strategy_id, venue=fill.venue, symbol=fill.symbol, mode=fill.mode,
                quantity=qty, entry_price=entry_price, exit_price=fill.price, entry_time=entry_time,
                exit_time=fill.ts, fees_usd=fill.fee_usd, realized_pnl=realized, exit_reason=exit_reason,
                exit_client_order_id=fill.client_order_id, is_virtual=fill.mode == Mode.PAPER,
                sub_strategy=p.sub_strategy or sub_strategy,
            )
        p.fees_paid += fill.fee_usd
        p.last_price = fill.price if p.last_price is None else p.last_price
        p.updated_at = fill.ts
        self.repo.upsert_position(p)
        return realized

    def _journal_exit(self, p: Position, fill: Fill, qty: float, reason: str, unsellable_qty: float = 0.0) -> None:
        """Merge exit fills into the trade cycle; journal the trade once the position is flat."""
        cyc = TradeCycle.from_dict(p.cycle)
        if cyc is None:
            log.warning("exit fill without a recorded trade cycle (%s %s): not journaled", p.strategy_id, p.symbol)
            return
        cyc.add_exit(qty, fill.price, fill.fee_usd, fill.client_order_id, reason, fill.ts)
        # remainder below the venue's lot step cannot be sold (fee charged in the base asset): trade is closed
        dust = p.quantity * fill.price < DUST_USD or p.quantity < unsellable_qty - 1e-15
        if p.quantity <= 1e-10 or dust:
            trade = close_cycle(cyc, mode=fill.mode.value, strategy_id=p.strategy_id, venue=p.venue, symbol=p.symbol)
            if not self.repo.insert_journal_trade(trade.to_dict(), fill.ts):
                log.warning("journal trade %s already recorded", trade.trade_id)
            p.cycle = None
        else:
            p.cycle = cyc.to_dict()

    def set_protection(self, strategy_id: str, venue: str, symbol: str,
                       stop: float | None, take_profit: float | None) -> None:
        p = self.get(strategy_id, venue, symbol)
        p.stop_price = stop
        p.take_profit = take_profit
        self.repo.upsert_position(p)

    def apply_funding(self, strategy_id: str, venue: str, symbol: str, amount_usd: float, ts: datetime) -> None:
        """Funding paid (positive) or received (negative) on a leveraged position: realized PnL."""
        p = self.get(strategy_id, venue, symbol)
        p.funding_paid += amount_usd
        p.realized_pnl -= amount_usd
        cyc = TradeCycle.from_dict(p.cycle)
        if cyc is not None:
            cyc.funding += amount_usd
            p.cycle = cyc.to_dict()
        p.updated_at = ts
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
