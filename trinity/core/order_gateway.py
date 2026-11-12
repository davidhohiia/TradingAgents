"""Per-strategy order gateway.

Bots never get an exchange adapter with trading rights. They get an OrderGateway
bound to their own strategy id, which forwards everything to the central execution
engine (and therefore through the Global Risk Manager).
"""

from __future__ import annotations

from trinity.core.execution_engine import ExecutionEngine, FillCallback
from trinity.core.models import OrderIntent, OrderRecord, Position, Side, SubmitResult


class StrategyMismatch(PermissionError):
    pass


class OrderGateway:
    def __init__(self, engine: ExecutionEngine, strategy_id: str, venue: str) -> None:
        self._engine = engine
        self.strategy_id = strategy_id
        self.venue = venue

    @property
    def mode(self):  # noqa: ANN201
        return self._engine.mode

    async def submit(self, intent: OrderIntent) -> SubmitResult:
        if intent.strategy_id != self.strategy_id:
            raise StrategyMismatch(f"gateway for {self.strategy_id} cannot submit for {intent.strategy_id}")
        if intent.venue != self.venue:
            raise StrategyMismatch(f"gateway for {self.strategy_id} is bound to venue {self.venue}")
        return await self._engine.submit(intent)

    async def cancel(self, client_order_id: str, reason: str = "") -> bool:
        rec = self._engine.repo.get_order(client_order_id)
        if rec is None or rec.strategy_id != self.strategy_id:
            return False
        return await self._engine.cancel(client_order_id, reason)

    async def cancel_all(self, *, side: Side | None = None, symbol: str | None = None, reason: str = "") -> int:
        return await self._engine.cancel_where(strategy_id=self.strategy_id, side=side, symbol=symbol, reason=reason)

    async def sync(self) -> int:
        return await self._engine.sync_orders(self.strategy_id)

    def active_orders(self, symbol: str | None = None) -> list[OrderRecord]:
        return self._engine.repo.list_active_orders(self._engine.mode, strategy_id=self.strategy_id, symbol=symbol)

    def get_order(self, client_order_id: str) -> OrderRecord | None:
        rec = self._engine.repo.get_order(client_order_id)
        return rec if rec and rec.strategy_id == self.strategy_id else None

    def position(self, symbol: str) -> Position:
        return self._engine.positions.get(self.strategy_id, self.venue, symbol)

    def on_fill(self, cb: FillCallback) -> None:
        self._engine.on_fill(self.strategy_id, cb)

    def capital(self):  # noqa: ANN201
        eng = self._engine
        realized = eng.positions.realized_by_strategy().get(self.strategy_id, 0.0)
        return eng.allocator.compute(
            self.strategy_id, eng.positions.all(), eng.repo.list_active_orders(eng.mode), realized, eng._fee_rates()
        )

    @property
    def max_risk_per_trade_pct(self) -> float:
        return self._engine.config.risk.max_risk_per_trade_pct

    def risk_locked(self) -> list[str]:
        return self._engine.risk.blocking_locks()

    def venue_ok(self) -> bool:
        return self._engine.breakers.allow(self.venue) and self._engine.venue_connected.get(self.venue, False)
