"""Capital allocator.

The database (positions + active orders + current allocations) is the source of truth,
so the allocation is reconstructed exactly after a crash. Rules:

* each strategy can only use its own allocation; the reserve is never allocated here;
* reinvestment: with ``reinvest_profits`` (default in 3.0) realized profits AND losses
  change the strategy capital (compounding). Without it only losses reduce capital;
* a client order id can hold only one reservation (no double allocation);
* available capital never becomes negative;
* leveraged positions tie up margin (cost / leverage), never more than allowed by the
  LeverageGuard, which is disabled by default.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
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
    def __init__(self, config: AppConfig, allocation_fn: Callable[[str], float] | None = None,
                 all_allocations_fn: Callable[[], dict[str, float]] | None = None) -> None:
        self.config = config
        self._allocation_fn = allocation_fn
        self._all_fn = all_allocations_fn
        self._lock = threading.Lock()
        self._inflight: dict[str, tuple[str, float]] = {}

    def allocation(self, strategy_id: str) -> float:
        if self._allocation_fn is not None:
            return float(self._allocation_fn(strategy_id))
        return self.config.allocation(strategy_id)

    def strategies(self) -> list[str]:
        if self._all_fn is not None:
            return list(self._all_fn())
        return list(self.config.capital.allocations)

    def effective_capital(self, strategy_id: str, realized_pnl: float) -> float:
        alloc = self.allocation(strategy_id)
        if alloc <= 0:
            return 0.0  # unfunded strategies (arbitrage) never trade, even with "profits"
        if self.config.capital.reinvest_profits:
            return max(alloc + realized_pnl, 0.0)
        return max(alloc + min(realized_pnl, 0.0), 0.0)

    def compute(
        self,
        strategy_id: str,
        positions: list[Position],
        active_orders: list[OrderRecord],
        realized_pnl: float,
        fee_rate_of: dict[str, float] | None = None,
    ) -> StrategyCapital:
        fee_rate_of = fee_rate_of or {}
        deployed = sum(p.margin_used for p in positions if p.strategy_id == strategy_id and p.is_open)
        reserved = 0.0
        db_ids = set()
        for o in active_orders:
            if o.strategy_id != strategy_id or o.side != Side.BUY:
                continue
            db_ids.add(o.client_order_id)
            price = o.price if o.price is not None else (o.avg_fill_price or 0.0)
            reserved += o.remaining * price * (1 + fee_rate_of.get(o.symbol, 0.0)) / max(o.leverage, 1.0)
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
            for sid in self.strategies()
        }
