"""Capital allocator.

The database (positions + active orders) is the source of truth, so the allocation
is always reconstructed correctly after a crash. Rules:

* Each strategy can only use its own allocation. The reserve is never allocated.
* Realized losses REDUCE the usable capital; realized profits do NOT increase it
  (no automatic growth of trading capital).
* A client order id can hold only one reservation (no double allocation).
* Available capital can never become negative: reservations that do not fit are refused.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from trinity.config.settings import AppConfig
from trinity.core.models import OrderRecord, Position, Side


@dataclass(frozen=True)
class StrategyCapital:
    strategy_id: str
    allocation: float
    realized_pnl: float
    effective_capital: float
    deployed: float
    reserved: float
    available: float


class CapitalAllocator:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._inflight: dict[str, tuple[str, float]] = {}

    @property
    def reserve_usd(self) -> float:
        return self.config.capital.reserve_usd

    def allocation(self, strategy_id: str) -> float:
        return self.config.allocation(strategy_id)

    def effective_capital(self, strategy_id: str, realized_pnl: float) -> float:
        return max(self.allocation(strategy_id) + min(realized_pnl, 0.0), 0.0)

    def compute(
        self,
        strategy_id: str,
        positions: list[Position],
        active_orders: list[OrderRecord],
        realized_pnl: float,
        fee_rate_of: dict[str, float] | None = None,
    ) -> StrategyCapital:
        fee_rate_of = fee_rate_of or {}
        deployed = sum(p.cost_basis for p in positions if p.strategy_id == strategy_id and p.is_open)
        reserved = 0.0
        db_ids = set()
        for o in active_orders:
            if o.strategy_id != strategy_id or o.side != Side.BUY:
                continue
            db_ids.add(o.client_order_id)
            price = o.price if o.price is not None else (o.avg_fill_price or 0.0)
            reserved += o.remaining * price * (1 + fee_rate_of.get(o.symbol, 0.0))
        with self._lock:
            reserved += sum(
                amt for cid, (sid, amt) in self._inflight.items() if sid == strategy_id and cid not in db_ids
            )
        eff = self.effective_capital(strategy_id, realized_pnl)
        available = max(eff - deployed - reserved, 0.0)
        return StrategyCapital(
            strategy_id, self.allocation(strategy_id), realized_pnl, eff, deployed, reserved, available
        )

    def reserve_inflight(self, strategy_id: str, client_order_id: str, amount: float, available: float) -> bool:
        """Reserve capital for an order between risk approval and DB persistence."""
        if amount < 0:
            return False
        with self._lock:
            if client_order_id in self._inflight:
                return False  # duplicate reservation for the same order
            pending = sum(a for sid, a in self._inflight.values() if sid == strategy_id)
            if amount > available - pending + 1e-9:
                return False
            self._inflight[client_order_id] = (strategy_id, amount)
            return True

    def release_inflight(self, client_order_id: str) -> None:
        with self._lock:
            self._inflight.pop(client_order_id, None)

    def summary(
        self, positions: list[Position], active_orders: list[OrderRecord], realized: dict[str, float]
    ) -> dict[str, StrategyCapital]:
        return {
            sid: self.compute(sid, positions, active_orders, realized.get(sid, 0.0))
            for sid in self.config.capital.allocations
        }
