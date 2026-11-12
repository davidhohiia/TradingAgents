"""Virtual (paper-only) arbitrage execution with per-venue inventory.

No real orders and no transfers are ever made here. Results are VIRTUAL.
Both legs are assumed to fill at the computed VWAPs plus the configured slippage,
which is optimistic for real cross-exchange execution (legging risk).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from trinity.bots.arbitrage.scanner import Opportunity


@dataclass
class VirtualBook:
    balances: dict[str, dict[str, float]] = field(default_factory=dict)  # venue -> asset -> qty
    trades: int = 0
    net_pnl: float = 0.0
    fees: float = 0.0
    skipped_no_inventory: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ArbitrageSimulator:
    def __init__(self, virtual_capital_usd: float, venues: list[str], state: dict[str, Any] | None = None,
                 n_bases: int = 2) -> None:
        """Virtual capital is split equally per venue into USDT and each base asset
        (inventory-based arbitrage needs both legs pre-funded on every venue)."""
        self.capital = virtual_capital_usd
        self.venues = venues
        self.n_bases = max(n_bases, 1)
        self.per_asset_usd = virtual_capital_usd / max(len(venues), 1) / (1 + self.n_bases)
        self.book = VirtualBook(**state) if state else VirtualBook()
        for v in venues:
            self.book.balances.setdefault(v, {"USDT": self.per_asset_usd})

    def seed_inventory(self, base: str, price: float, n_bases: int | None = None) -> None:
        for v in self.venues:
            bal = self.book.balances[v]
            if base not in bal and price > 0:
                bal[base] = self.per_asset_usd / price

    def try_execute(self, opp: Opportunity) -> bool:
        if not opp.accepted or opp.buy_vwap is None or opp.sell_vwap is None:
            return False
        base = opp.symbol.split("/")[0]
        buy_bal = self.book.balances[opp.buy_venue]
        sell_bal = self.book.balances[opp.sell_venue]
        cost = opp.quantity * opp.buy_vwap
        buy_fee = opp.fees * cost / (cost + opp.quantity * opp.sell_vwap)
        if buy_bal.get("USDT", 0.0) < cost + buy_fee or sell_bal.get(base, 0.0) < opp.quantity:
            self.book.skipped_no_inventory += 1
            return False
        sell_fee = opp.fees - buy_fee
        half_slip = opp.slippage / 2
        buy_bal["USDT"] -= cost + buy_fee + half_slip
        buy_bal[base] = buy_bal.get(base, 0.0) + opp.quantity
        sell_bal[base] -= opp.quantity
        sell_bal["USDT"] = sell_bal.get("USDT", 0.0) + opp.quantity * opp.sell_vwap - sell_fee - half_slip
        self.book.trades += 1
        self.book.fees += opp.fees
        # realized virtual PnL excludes the latency/transfer *reserves* only if they did not occur;
        # we book the conservative net figure.
        self.book.net_pnl += opp.net_profit
        return True
