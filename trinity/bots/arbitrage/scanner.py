"""Cross-exchange arbitrage scanner (calculation only).

Net profit = sell proceeds - buy cost - buy fee - sell fee - slippage - other execution costs
(amortized transfer/rebalancing cost and a latency-risk charge).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from trinity.config.settings import ArbitrageConfig
from trinity.core.models import OrderBook


@dataclass
class Opportunity:
    symbol: str
    buy_venue: str
    sell_venue: str
    ts: datetime
    quantity: float = 0.0
    buy_vwap: float | None = None
    sell_vwap: float | None = None
    gross_profit: float = 0.0
    fees: float = 0.0
    slippage: float = 0.0
    transfer_cost: float = 0.0
    latency_risk: float = 0.0
    net_profit: float = 0.0
    net_bps: float = 0.0
    reasons: list[str] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return not self.reasons

    def as_row(self, virtual_executed: bool = False) -> dict[str, object]:
        return {
            "ts": self.ts.isoformat(), "symbol": self.symbol, "buy_venue": self.buy_venue,
            "sell_venue": self.sell_venue, "quantity": self.quantity, "buy_vwap": self.buy_vwap,
            "sell_vwap": self.sell_vwap, "gross_profit": self.gross_profit, "fees": self.fees,
            "slippage": self.slippage, "transfer_cost": self.transfer_cost, "latency_risk": self.latency_risk,
            "net_profit": self.net_profit, "accepted": int(self.accepted),
            "reject_reasons": "; ".join(self.reasons), "virtual_executed": int(virtual_executed),
        }


def vwap_for_qty(levels: list[tuple[float, float]], qty: float) -> tuple[float | None, float]:
    """VWAP to fill qty by walking the book. Returns (vwap or None if depth insufficient, fillable qty)."""
    remaining, cost, filled = qty, 0.0, 0.0
    for price, amount in levels:
        take = min(remaining, amount)
        cost += take * price
        filled += take
        remaining -= take
        if remaining <= 1e-12:
            return cost / qty, qty
    return None, filled


@dataclass(frozen=True)
class VenueLimits:
    min_qty: float = 0.0
    min_notional: float = 0.0


class ArbitrageScanner:
    def __init__(self, cfg: ArbitrageConfig, limits: dict[tuple[str, str], VenueLimits] | None = None) -> None:
        self.cfg = cfg
        self.limits = limits or {}

    def evaluate(
        self, symbol: str, books: dict[str, OrderBook | BaseException], now: datetime
    ) -> list[Opportunity]:
        c = self.cfg
        out: list[Opportunity] = []
        venues = list(books)
        base = symbol.split("/")[0]
        for buy_v in venues:
            for sell_v in venues:
                if buy_v == sell_v:
                    continue
                opp = Opportunity(symbol, buy_v, sell_v, now)
                out.append(opp)
                bb, sb = books[buy_v], books[sell_v]
                if isinstance(bb, BaseException) or isinstance(sb, BaseException):
                    bad = buy_v if isinstance(bb, BaseException) else sell_v
                    opp.reasons.append(f"exchange unavailable: {bad}")
                    continue
                for v, b in ((buy_v, bb), (sell_v, sb)):
                    age_ms = (now - b.ts).total_seconds() * 1000
                    if age_ms > c.max_quote_age_ms:
                        opp.reasons.append(f"stale quotes on {v}: {age_ms:.0f}ms")
                if abs((bb.ts - sb.ts).total_seconds() * 1000) > c.max_clock_skew_ms:
                    opp.reasons.append("order book snapshots too far apart in time")
                if not bb.asks or not sb.bids:
                    opp.reasons.append("empty order book side")
                    continue
                ask = bb.asks[0][0]
                qty = c.trade_size_usd / ask
                opp.quantity = qty
                buy_vwap, buy_fill = vwap_for_qty(bb.asks, qty)
                sell_vwap, sell_fill = vwap_for_qty(sb.bids, qty)
                if buy_vwap is None or sell_vwap is None:
                    opp.reasons.append(
                        f"insufficient depth (buy fillable {buy_fill:.6f}, sell fillable {sell_fill:.6f}, need {qty:.6f})")
                    continue
                opp.buy_vwap, opp.sell_vwap = buy_vwap, sell_vwap
                buy_cost, proceeds = qty * buy_vwap, qty * sell_vwap
                opp.gross_profit = proceeds - buy_cost
                opp.fees = buy_cost * c.taker_fee(buy_v) + proceeds * c.taker_fee(sell_v)
                opp.slippage = (buy_cost + proceeds) * c.slippage_bps / 10_000
                transfer = c.transfer_fees.get(base, 0.0) * sell_vwap + c.transfer_fees.get("USDT", 0.0)
                opp.transfer_cost = transfer / c.transfer_amortization_trades
                latency_s = c.assumed_latency_ms / 1000
                opp.latency_risk = buy_cost * c.volatility_per_second_bps / 10_000 * math.sqrt(max(latency_s, 0))
                opp.net_profit = (opp.gross_profit - opp.fees - opp.slippage - opp.transfer_cost
                                  - opp.latency_risk)
                opp.net_bps = opp.net_profit / buy_cost * 10_000 if buy_cost else 0.0
                for v in (buy_v, sell_v):
                    lim = self.limits.get((v, symbol), VenueLimits())
                    if qty < lim.min_qty or buy_cost < lim.min_notional:
                        opp.reasons.append(f"below minimum order size on {v}")
                if opp.latency_risk > c.max_execution_risk_usd:
                    opp.reasons.append(f"execution risk {opp.latency_risk:.2f} > {c.max_execution_risk_usd}")
                if opp.net_profit <= 0:
                    opp.reasons.append(f"negative expected net profit {opp.net_profit:.4f}")
                elif opp.net_profit < c.min_net_profit_usd or opp.net_bps < c.min_net_profit_bps:
                    opp.reasons.append(f"net {opp.net_profit:.4f} USD / {opp.net_bps:.1f}bps below minimum")
        return out
