"""Exposure aggregation: positions + pending buy orders, by strategy / symbol / correlation group."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

from trinity.core.models import OrderRecord, Position, Side


@dataclass
class ExposureSnapshot:
    total: float = 0.0
    by_strategy: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    by_group: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    by_strategy_symbol: dict[tuple[str, str], float] = field(default_factory=lambda: defaultdict(float))
    position_qty: dict[tuple[str, str], float] = field(default_factory=lambda: defaultdict(float))
    pending_sell_qty: dict[tuple[str, str], float] = field(default_factory=lambda: defaultdict(float))
    open_positions: set[tuple[str, str]] = field(default_factory=set)
    open_orders: int = 0


class ExposureManager:
    """Exposure = market value of open positions + notional of active (unfilled part of) BUY orders."""

    def __init__(self, group_of: Callable[[str], str]) -> None:
        self.group_of = group_of

    def snapshot(self, positions: list[Position], active_orders: list[OrderRecord]) -> ExposureSnapshot:
        snap = ExposureSnapshot()
        for p in positions:
            if not p.is_open:
                continue
            value = p.market_value
            key = (p.strategy_id, p.symbol)
            snap.total += value
            snap.by_strategy[p.strategy_id] += value
            snap.by_group[self.group_of(p.symbol)] += value
            snap.by_strategy_symbol[key] += value
            snap.position_qty[key] += p.quantity
            snap.open_positions.add(key)
        for o in active_orders:
            snap.open_orders += 1
            key = (o.strategy_id, o.symbol)
            if o.side == Side.BUY:
                price = o.price if o.price is not None else (o.avg_fill_price or 0.0)
                pending = o.remaining * price
                snap.total += pending
                snap.by_strategy[o.strategy_id] += pending
                snap.by_group[self.group_of(o.symbol)] += pending
                snap.by_strategy_symbol[key] += pending
            else:
                snap.pending_sell_qty[key] += o.remaining
        return snap
