"""AI modules (deterministic analyzer, anomaly detector, performance analyzer) and monitoring."""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
import pytest

from trinity.ai.anomaly_detector import AnomalyDetector
from trinity.ai.market_analyzer import MarketAnalyzer
from trinity.ai.performance_analyzer import PerformanceAnalyzer
from trinity.core.models import MarketRegime, Mode, OrderBook, Side
from trinity.monitoring.health import check_health
from trinity.tests.test_grid import crash, make_df, ranging


def test_market_analyzer_deterministic_and_no_invented_data():
    a = MarketAnalyzer()
    res = a.analyze("BTC/USDT", make_df(crash()))
    assert res.regime in (MarketRegime.STRONG_BEAR, MarketRegime.BEAR, MarketRegime.HIGH_VOLATILITY)
    assert "Basis:" in res.explanation and res.evidence["adx"] is not None
    empty = a.analyze("BTC/USDT", pd.DataFrame())
    assert empty.regime == MarketRegime.UNKNOWN and "No market data" in empty.explanation
    short = a.analyze("BTC/USDT", make_df(ranging(20)))
    assert short.regime == MarketRegime.UNKNOWN and short.warnings


async def test_market_analyzer_works_when_external_ai_is_down():
    async def broken(_text):
        raise ConnectionError("LLM API unavailable")

    a = MarketAnalyzer(explainer=broken)
    res = a.analyze("BTC/USDT", make_df(ranging()))
    assert await a.explain_with_llm(res) == res.explanation


def test_anomaly_detector_books(system):
    d = AnomalyDetector()
    now = system.clock.now()
    ok = OrderBook("X", [(99.99, 1)], [(100.01, 1)], now, "v")
    for _ in range(25):
        assert d.check_book(ok, now) == []
    wide = OrderBook("X", [(99.0, 1)], [(101.0, 1)], now, "v")
    assert any("UNUSUAL_SPREAD" in i for i in d.check_book(wide, now))
    stale = OrderBook("X", [(99.99, 1)], [(100.01, 1)], now - timedelta(minutes=5), "v")
    assert any("STALE_PRICE" in i for i in d.check_book(stale, now))
    crossed = OrderBook("X", [(101, 1)], [(100, 1)], now, "v")
    assert any("CROSSED_BOOK" in i for i in d.check_book(crossed, now))
    assert d.check_book(OrderBook("X", [], [(1, 1)], now, "v"), now)


def test_anomaly_detector_api_errors_and_executions(system):
    d = AnomalyDetector()
    now = system.clock.now()
    for i in range(10):
        d.record_api_call("binance", i % 2 == 0, now)
    assert d.check_api("binance", now)
    assert any("LIMIT_VIOLATION" in i for i in d.check_execution(
        side=Side.BUY, ordered_qty=1, filled_qty=1, fill_price=101, limit_price=100, reference_price=100))
    assert any("OVERFILL" in i for i in d.check_execution(
        side=Side.SELL, ordered_qty=1, filled_qty=2, fill_price=100, limit_price=None, reference_price=100))
    assert any("HIGH_SLIPPAGE" in i for i in d.check_execution(
        side=Side.BUY, ordered_qty=1, filled_qty=1, fill_price=103, limit_price=None, reference_price=100))


def test_performance_analyzer_needs_sample_and_never_auto_applies(system):
    pa = PerformanceAnalyzer(system.repo)
    recs = pa.recommendations("crypto_grid", Mode.PAPER)
    assert "Keep collecting data" in recs[0].message and not recs[0].auto_apply
    from trinity.core.models import utcnow

    for i in range(60):
        pnl = 0.5 if i < 30 else -0.4
        system.repo.upsert_trade_fill(strategy_id="crypto_grid", venue="crypto", symbol="BTC/USDT", mode=Mode.PAPER,
                                      quantity=1, entry_price=1, exit_price=1, entry_time=utcnow(), exit_time=utcnow(),
                                      fees_usd=0.6, realized_pnl=pnl, exit_reason="", exit_client_order_id=f"x{i}",
                                      is_virtual=True)
    perf = pa.strategy_performance("crypto_grid", Mode.PAPER)
    assert perf.trades == 60 and perf.win_rate == pytest.approx(0.5)
    recs = pa.recommendations("crypto_grid", Mode.PAPER)
    assert all(not r.auto_apply for r in recs)
    assert any("degradation" in r.message or "Fees" in r.message for r in recs)
    notes = pa.compare_with_backtest(perf, {"win_rate": 0.8, "avg_trade": 0.2, "profit_factor": 2.0})
    assert notes


def test_health_detects_stale_heartbeat_and_breakers(system):
    system.repo.upsert_bot_health("crypto_grid", "PAPER MODE", Mode.PAPER, None, 0, ts=system.clock.now())
    rep = check_health(system.repo, system.clock.now(), {"crypto_grid": 30})
    assert rep.ok
    rep = check_health(system.repo, system.clock.now() + timedelta(hours=1), {"crypto_grid": 30},
                       {"crypto": {"state": "open", "last_error": "timeout"}})
    assert not rep.ok and len(rep.problems) == 2


async def test_watchdog_alerts_once_per_cooldown(system, config):
    from trinity.monitoring.watchdog import Watchdog
    from trinity.tests.test_portfolio import make_pm

    pm, notifier = make_pm(system, config)
    await pm.start()
    await pm.tick()
    system.clock.advance(3 * 3600)  # bots silent for 3 hours -> stale heartbeat
    wd = Watchdog(pm, system.repo.path.parent)
    p1 = await wd.check()
    p2 = await wd.check()
    assert p1 and p2
    assert len([e for e, _, _ in notifier.sent if e == "watchdog"]) == len(p1)  # no alert spam
