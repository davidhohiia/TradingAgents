"""Anomaly detection: unusual spreads, stale prices, API errors, abnormal executions.

Deterministic rules on observed data; it never invents missing data.
"""

from __future__ import annotations

import statistics
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta

from trinity.core.models import Candle, OrderBook, Side

TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400, "1Day": 86400}


@dataclass
class Anomaly:
    kind: str
    message: str
    severity: str = "WARNING"


class AnomalyDetector:
    def __init__(self, spread_window: int = 200, spread_multiplier: float = 5.0, max_spread_pct: float = 0.01) -> None:
        self.spread_hist: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=spread_window))
        self.spread_multiplier = spread_multiplier
        self.max_spread_pct = max_spread_pct
        self.api_calls: dict[str, deque[tuple[datetime, bool]]] = defaultdict(lambda: deque(maxlen=500))
        self.log: deque[Anomaly] = deque(maxlen=500)

    def _add(self, kind: str, msg: str, severity: str = "WARNING") -> str:
        self.log.append(Anomaly(kind, msg, severity))
        return f"{kind}: {msg}"

    # ------------------------------------------------------------------ prices
    def check_book(self, book: OrderBook, now: datetime, max_age_s: float = 30.0) -> list[str]:
        issues: list[str] = []
        key = f"{book.venue}:{book.symbol}"
        if not book.bids or not book.asks:
            return [self._add("EMPTY_BOOK", f"{key} has an empty side")]
        bid, ask = book.best_bid, book.best_ask
        assert bid is not None and ask is not None
        if bid <= 0 or ask <= 0:
            issues.append(self._add("BAD_PRICE", f"{key} non-positive price"))
        if bid >= ask:
            issues.append(self._add("CROSSED_BOOK", f"{key} bid {bid} >= ask {ask}"))
        age = (now - book.ts).total_seconds()
        if age > max_age_s:
            issues.append(self._add("STALE_PRICE", f"{key} order book is {age:.0f}s old"))
        spread = book.spread_pct
        if spread is not None and spread > 0:
            hist = self.spread_hist[key]
            if len(hist) >= 20:
                med = statistics.median(hist)
                if med > 0 and spread > med * self.spread_multiplier:
                    issues.append(self._add("UNUSUAL_SPREAD", f"{key} spread {spread:.4%} vs median {med:.4%}"))
            if spread > self.max_spread_pct:
                issues.append(self._add("WIDE_SPREAD", f"{key} spread {spread:.3%} > {self.max_spread_pct:.2%}"))
            hist.append(spread)
        return issues

    def check_candles(self, candles: list[Candle], now: datetime, timeframe: str, max_bars_old: float = 3.0) -> list[str]:
        if not candles:
            return [self._add("NO_DATA", "no candles returned")]
        issues = []
        last = candles[-1]
        tf = TF_SECONDS.get(timeframe, 60)
        if (now - last.ts).total_seconds() > tf * max_bars_old and timeframe not in ("1d", "1Day"):
            issues.append(self._add("STALE_CANDLES", f"last candle {last.ts.isoformat()} too old"))
        for c in candles[-5:]:
            if c.low <= 0 or c.high < c.low or not (c.low <= c.close <= c.high):
                issues.append(self._add("BAD_CANDLE", f"inconsistent OHLC at {c.ts.isoformat()}"))
                break
        return issues

    # ------------------------------------------------------------------ API errors
    def record_api_call(self, venue: str, ok: bool, now: datetime) -> None:
        self.api_calls[venue].append((now, ok))

    def api_error_rate(self, venue: str, now: datetime, window_s: float = 600) -> float:
        cutoff = now - timedelta(seconds=window_s)
        calls = [ok for ts, ok in self.api_calls[venue] if ts >= cutoff]
        if not calls:
            return 0.0
        return 1 - sum(calls) / len(calls)

    def check_api(self, venue: str, now: datetime, threshold: float = 0.3) -> list[str]:
        rate = self.api_error_rate(venue, now)
        if rate >= threshold:
            return [self._add("API_ERRORS", f"{venue} error rate {rate:.0%} in last 10 min", "CRITICAL")]
        return []

    # ------------------------------------------------------------------ executions
    def check_execution(self, *, side: Side, ordered_qty: float, filled_qty: float, fill_price: float,
                        limit_price: float | None, reference_price: float, max_slippage_pct: float = 0.01) -> list[str]:
        issues = []
        if filled_qty > ordered_qty * (1 + 1e-6):
            issues.append(self._add("OVERFILL", f"filled {filled_qty} > ordered {ordered_qty}", "CRITICAL"))
        if limit_price is not None:
            if side == Side.BUY and fill_price > limit_price * (1 + 1e-6):
                issues.append(self._add("LIMIT_VIOLATION", f"buy filled {fill_price} above limit {limit_price}", "CRITICAL"))
            if side == Side.SELL and fill_price < limit_price * (1 - 1e-6):
                issues.append(self._add("LIMIT_VIOLATION", f"sell filled {fill_price} below limit {limit_price}", "CRITICAL"))
        if reference_price > 0:
            slip = (fill_price - reference_price) / reference_price * (1 if side == Side.BUY else -1)
            if slip > max_slippage_pct:
                issues.append(self._add("HIGH_SLIPPAGE", f"slippage {slip:.2%} vs reference {reference_price}"))
        return issues

    def recent(self, n: int = 20) -> list[Anomaly]:
        return list(self.log)[-n:]
