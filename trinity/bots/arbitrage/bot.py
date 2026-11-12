"""Arbitrage AI bot: SCAN + VIRTUAL execution only.

Real arbitrage execution is intentionally NOT implemented. No automatic transfers
between exchanges. Enabling real execution requires a separate review and new code.
"""

from __future__ import annotations

import asyncio
from typing import Any

from trinity.ai.anomaly_detector import AnomalyDetector
from trinity.bots.arbitrage.scanner import ArbitrageScanner, VenueLimits
from trinity.bots.arbitrage.simulator import ArbitrageSimulator
from trinity.bots.base import BaseBot
from trinity.config.settings import ArbitrageConfig
from trinity.core.models import BotStatus, OrderBook
from trinity.exchanges.base_exchange import ExchangeUnavailable, MarketDataSource


class ArbitrageBot(BaseBot):
    bot_type = "arbitrage"

    def __init__(self, cfg: ArbitrageConfig, sources: dict[str, MarketDataSource],
                 limits: dict[tuple[str, str], VenueLimits] | None = None,
                 anomaly: AnomalyDetector | None = None, fetch_timeout: float = 5.0, **kwargs: Any) -> None:
        kwargs.setdefault("market", next(iter(sources.values()), None))
        super().__init__(strategy_id=cfg.strategy_id, gateway=None, loop_interval=cfg.loop_interval_seconds, **kwargs)
        if cfg.mode != "scan_only":
            raise ValueError("arbitrage bot supports only scan_only mode")
        self.cfg = cfg
        self.sources = sources
        self.scanner = ArbitrageScanner(cfg, limits)
        self.anomaly = anomaly or AnomalyDetector()
        self.fetch_timeout = fetch_timeout
        state = self.load_state()
        self.sim = ArbitrageSimulator(cfg.virtual_capital_usd, list(sources), state.get("virtual"), len(cfg.symbols))
        if self.sim.per_asset_usd < cfg.trade_size_usd:
            self.log.warning("virtual inventory per asset %.0f < trade size %.0f: virtual trades will be skipped",
                             self.sim.per_asset_usd, cfg.trade_size_usd)
        self.stats: dict[str, Any] = state.get("stats", {"scans": 0, "accepted": 0, "rejected": 0})

    def status(self) -> BotStatus:
        base = super().status()
        if base in (BotStatus.STOPPED, BotStatus.CONNECTION_ERROR, BotStatus.NO_DATA):
            return base
        return BotStatus.SCAN_ONLY

    async def _fetch(self, venue: str, symbol: str) -> OrderBook | BaseException:
        try:
            return await asyncio.wait_for(
                self.sources[venue].fetch_order_book(symbol, self.cfg.orderbook_depth), self.fetch_timeout)
        except (TimeoutError, Exception) as exc:  # noqa: BLE001
            self.anomaly.record_api_call(venue, False, self.clock.now())
            return exc
        finally:
            pass

    async def step(self) -> None:
        if len(self.sources) < 2:
            self.info = {"note": "need at least 2 exchanges (ARB_EXCHANGE_IDS)"}
            return
        now = self.clock.now()
        summary: dict[str, Any] = {}
        all_failed = True
        for symbol in self.cfg.symbols:
            venues = list(self.sources)
            results = await asyncio.gather(*(self._fetch(v, symbol) for v in venues))
            books = dict(zip(venues, results, strict=True))
            for v, b in books.items():
                if isinstance(b, OrderBook):
                    all_failed = False
                    self.anomaly.record_api_call(v, True, now)
                    if b.mid:
                        self.sim.seed_inventory(symbol.split("/")[0], b.mid, len(self.cfg.symbols))
            opps = self.scanner.evaluate(symbol, books, now)
            self.stats["scans"] += 1
            accepted = [o for o in opps if o.accepted]
            self.stats["accepted"] += len(accepted)
            self.stats["rejected"] += len(opps) - len(accepted)
            for o in sorted(accepted, key=lambda x: -x.net_profit):
                executed = self.sim.try_execute(o)
                self.repo.add_arbitrage_opportunity(o.as_row(virtual_executed=executed))
            valued = [o for o in opps if o.buy_vwap is not None]
            best = max(valued, key=lambda o: o.net_profit) if valued else (opps[0] if opps else None)
            if best is not None and not best.accepted:
                self.repo.add_arbitrage_opportunity(best.as_row())
            summary[symbol] = {
                "best": None if best is None else {
                    "buy": best.buy_venue, "sell": best.sell_venue, "net": round(best.net_profit, 4),
                    "gross": round(best.gross_profit, 4), "reasons": best.reasons},
                "accepted": len(accepted),
                "unavailable": [v for v, b in books.items() if not isinstance(b, OrderBook)],
            }
        self.save_state({"virtual": self.sim.book.to_dict(), "stats": self.stats})
        self.info = {"mode": "SCAN ONLY / VIRTUAL", "symbols": summary, "stats": self.stats,
                     "virtual_pnl": round(self.sim.book.net_pnl, 4), "virtual_trades": self.sim.book.trades}
        if all_failed:
            raise ExchangeUnavailable("all arbitrage exchanges unavailable")
