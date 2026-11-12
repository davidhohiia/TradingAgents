"""SYNTHETIC market data source for tests and the accelerated 24h simulation.

Prices are generated (geometric Brownian motion with regime switching).
This data is NOT real and must never be presented as real market results.
Supports failure injection: outages, random API errors, stale snapshots.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from trinity.core.clock import Clock
from trinity.core.models import AssetClass, Candle, OrderBook, Ticker
from trinity.exchanges.base_exchange import ExchangeUnavailable, MarketDataSource

TIMEFRAME_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400, "1Day": 86400}


@dataclass
class SymbolSpec:
    symbol: str
    start_price: float
    timeframe: str  # native bar timeframe exposed through fetch_ohlcv
    annual_vol: float = 0.6
    annual_drift: float = 0.0
    spread_bps: float = 2.0
    depth_levels: int = 20
    level_qty_usd: float = 25_000.0
    bar_volume: float = 1000.0
    history_bars: int = 400
    regimes: list[tuple[float, float]] = field(default_factory=list)  # (hours_from_start, annual_drift)


class SimulatedMarket(MarketDataSource):
    def __init__(
        self,
        name: str,
        asset_class: AssetClass,
        specs: list[SymbolSpec],
        clock: Clock,
        *,
        seed: int = 7,
        step_seconds: int = 60,
        error_rate: float = 0.0,
        stale_rate: float = 0.0,
        outages: list[tuple[datetime, datetime]] | None = None,
        session_check=None,
    ) -> None:
        self.name = name
        self.asset_class = asset_class
        self.clock = clock
        self.rng = random.Random(seed)
        self.step = step_seconds
        self.error_rate = error_rate
        self.stale_rate = stale_rate
        self.outages = outages or []
        self.session_check = session_check
        self.specs = {s.symbol: s for s in specs}
        self.start = clock.now()
        self._history: dict[str, list[Candle]] = {}
        self._path: dict[str, list[tuple[datetime, float]]] = {}
        self.calls = 0
        self.errors = 0
        for s in specs:
            self._history[s.symbol] = self._gen_history(s)
            last = self._history[s.symbol][-1].close if self._history[s.symbol] else s.start_price
            self._path[s.symbol] = [(self.start, last)]

    # ------------------------------------------------------------------ generation
    def _gen_history(self, s: SymbolSpec) -> list[Candle]:
        tf = TIMEFRAME_SECONDS[s.timeframe]
        dt_years = tf / (365 * 86400)
        sigma = s.annual_vol * math.sqrt(dt_years)
        price = s.start_price
        out: list[Candle] = []
        t0 = self.start - timedelta(seconds=tf * s.history_bars)
        t0 = t0.replace(second=0, microsecond=0)
        for i in range(s.history_bars):
            ts = t0 + timedelta(seconds=tf * i)
            ret = (s.annual_drift - 0.5 * s.annual_vol**2) * dt_years + sigma * self.rng.gauss(0, 1)
            close = price * math.exp(ret)
            hi = max(price, close) * (1 + abs(self.rng.gauss(0, sigma / 2)))
            lo = min(price, close) * (1 - abs(self.rng.gauss(0, sigma / 2)))
            vol = s.bar_volume * (0.5 + self.rng.random())
            out.append(Candle(ts, price, hi, lo, close, vol))
            price = close
        return out

    def _drift(self, s: SymbolSpec, now: datetime) -> float:
        hours = (now - self.start).total_seconds() / 3600
        drift = s.annual_drift
        for start_h, d in s.regimes:
            if hours >= start_h:
                drift = d
        return drift

    def _advance(self, symbol: str) -> None:
        s = self.specs[symbol]
        path = self._path[symbol]
        now = self.clock.now()
        dt_years = self.step / (365 * 86400)
        while path[-1][0] + timedelta(seconds=self.step) <= now:
            ts, price = path[-1]
            drift = self._drift(s, ts)
            ret = (drift - 0.5 * s.annual_vol**2) * dt_years + s.annual_vol * math.sqrt(dt_years) * self.rng.gauss(0, 1)
            path.append((ts + timedelta(seconds=self.step), price * math.exp(ret)))

    def price(self, symbol: str) -> float:
        self._advance(symbol)
        return self._path[symbol][-1][1]

    def _check_available(self, symbol: str) -> None:
        self.calls += 1
        now = self.clock.now()
        if symbol not in self.specs:
            raise ExchangeUnavailable(f"{self.name}: unknown symbol {symbol}")
        for start, end in self.outages:
            if start <= now < end:
                self.errors += 1
                raise ExchangeUnavailable(f"{self.name}: simulated outage")
        if self.error_rate and self.rng.random() < self.error_rate:
            self.errors += 1
            raise ExchangeUnavailable(f"{self.name}: simulated timeout")

    # ------------------------------------------------------------------ MarketDataSource
    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        self._check_available(symbol)
        s = self.specs[symbol]
        if timeframe != s.timeframe:
            raise ValueError(f"simulated {symbol} only provides {s.timeframe} bars")
        self._advance(symbol)
        tf = TIMEFRAME_SECONDS[timeframe]
        candles = list(self._history[symbol])
        # aggregate generated path into bars of the native timeframe
        bucket: dict[datetime, list[float]] = {}
        last_hist_close = candles[-1].ts + timedelta(seconds=tf) if candles else self.start
        for ts, px in self._path[symbol]:
            if ts < last_hist_close:
                continue
            epoch = int(ts.timestamp())
            b = datetime.fromtimestamp(epoch - epoch % tf, tz=ts.tzinfo)
            bucket.setdefault(b, []).append(px)
        for b in sorted(bucket):
            pxs = bucket[b]
            vol = s.bar_volume * (0.5 + self.rng.random()) * len(pxs) / max(tf / self.step, 1)
            candles.append(Candle(b, pxs[0], max(pxs), min(pxs), pxs[-1], vol))
        return candles[-limit:]

    async def fetch_order_book(self, symbol: str, depth: int = 20) -> OrderBook:
        self._check_available(symbol)
        s = self.specs[symbol]
        mid = self.price(symbol)
        half = mid * s.spread_bps / 20_000
        tick = mid * 0.0001
        bids, asks = [], []
        for i in range(min(depth, s.depth_levels)):
            qty = s.level_qty_usd / mid * (0.5 + self.rng.random())
            bids.append((mid - half - i * tick, qty))
            asks.append((mid + half + i * tick, qty))
        ts = self.clock.now()
        if self.stale_rate and self.rng.random() < self.stale_rate:
            ts = ts - timedelta(minutes=10)
        return OrderBook(symbol, bids, asks, ts, venue=self.name)

    async def fetch_ticker(self, symbol: str) -> Ticker:
        book = await self.fetch_order_book(symbol, 1)
        return Ticker(symbol, book.best_bid, book.best_ask, book.mid or 0.0, book.ts, self.name)

    async def is_market_open(self, now: datetime) -> bool:
        if self.session_check is not None:
            return self.session_check(now)
        return True

    def set_price(self, symbol: str, price: float) -> None:
        """Force the current price (tests: crash scenarios)."""
        self._advance(symbol)
        ts, _ = self._path[symbol][-1]
        self._path[symbol][-1] = (ts, price)
