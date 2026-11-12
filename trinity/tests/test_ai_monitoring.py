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
from trinity.tests.helpers import crash, make_df, ranging


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
    recs = pa.recommendations("crypto_momentum", Mode.PAPER)
    assert "Keep collecting data" in recs[0].message and not recs[0].auto_apply
    from trinity.core.models import utcnow

    for i in range(60):
        pnl = 0.5 if i < 30 else -0.4
        system.repo.upsert_trade_fill(strategy_id="crypto_momentum", venue="crypto", symbol="BTC/USDT", mode=Mode.PAPER,
                                      quantity=1, entry_price=1, exit_price=1, entry_time=utcnow(), exit_time=utcnow(),
                                      fees_usd=0.6, realized_pnl=pnl, exit_reason="", exit_client_order_id=f"x{i}",
                                      is_virtual=True)
    perf = pa.strategy_performance("crypto_momentum", Mode.PAPER)
    assert perf.trades == 60 and perf.win_rate == pytest.approx(0.5)
    recs = pa.recommendations("crypto_momentum", Mode.PAPER)
    assert all(not r.auto_apply for r in recs)
    assert any("degradation" in r.message or "Fees" in r.message for r in recs)
    notes = pa.compare_with_backtest(perf, {"win_rate": 0.8, "avg_trade": 0.2, "profit_factor": 2.0})
    assert notes


def test_health_detects_stale_heartbeat_and_breakers(system):
    system.repo.upsert_bot_health("crypto_momentum", "PAPER MODE", Mode.PAPER, None, 0, ts=system.clock.now())
    rep = check_health(system.repo, system.clock.now(), {"crypto_momentum": 30})
    assert rep.ok
    rep = check_health(system.repo, system.clock.now() + timedelta(hours=1), {"crypto_momentum": 30},
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


# ---------------------------------------------------------------- TRINITY 3.0 monitoring / analytics
async def test_api_monitor_times_calls_and_flags_problems(system):
    from trinity.exchanges.base_exchange import ExchangeUnavailable
    from trinity.monitoring.api_monitor import ApiMonitor

    mon = ApiMonitor()
    proxy = mon.wrap(system.crypto_market, "sim")
    assert proxy.name == system.crypto_market.name  # transparent attributes
    for _ in range(12):
        await proxy.fetch_order_book("BTC/USDT")
    system.crypto_market.outages = [(system.clock.now(), system.clock.now() + timedelta(hours=1))]
    for _ in range(8):
        with pytest.raises(ExchangeUnavailable):
            await proxy.fetch_order_book("BTC/USDT")
    st = mon.stats("sim")
    assert st["calls"] == 20 and st["errors"] == 8 and st["p50_ms"] is not None
    assert any("error rate" in p for p in mon.problems())
    mon.flush(system.repo, system.clock.now())
    assert system.repo.query("SELECT calls, errors FROM api_health")[0]["errors"] == 8


async def test_funding_accrual_and_liquidation_alert(system, config):
    from trinity.tests.test_portfolio import make_pm

    pm, notifier = make_pm(system, config)

    async def funding(symbol):
        return 0.001  # 0.1% / 8h (above the 0.05% limit)

    system.crypto.fetch_funding_rate = funding
    p = system.positions.get("crypto_momentum", "crypto", "BTC/USDT")
    p.quantity, p.avg_entry_price, p.last_price, p.leverage = 0.002, 60000, 60000, 1.5
    p.liquidation_price = 58000
    system.repo.set_kv("funding:paper:last", (system.clock.now() - timedelta(hours=8)).isoformat())
    await pm.accrue_funding()
    assert p.funding_paid == pytest.approx(0.002 * 60000 * 0.001)
    assert p.realized_pnl == pytest.approx(-p.funding_paid)
    assert system.repo.query("SELECT COUNT(*) FROM funding_payments")[0][0] == 1
    assert any(e == "funding" for e, _, _ in notifier.sent)
    assert any(e == "risk_limit" and "Liquidation" in t for e, t, _ in notifier.sent)


def test_per_strategy_stats_and_real_vs_virtual(system):
    from trinity.ai.performance_analyzer import StrategyAnalytics
    from trinity.core.models import utcnow

    def add(mode, sub, pnl, i):
        system.repo.upsert_trade_fill(strategy_id="crypto_momentum", venue="crypto", symbol="BTC/USDT", mode=mode,
                                      quantity=1, entry_price=1, exit_price=1, entry_time=utcnow(), exit_time=utcnow(),
                                      fees_usd=0.1, realized_pnl=pnl, exit_reason="", is_virtual=mode == Mode.PAPER,
                                      exit_client_order_id=f"{mode.value}{sub}{i}", sub_strategy=sub)

    for i in range(40):
        add(Mode.PAPER, "momentum", 1.0, i)
        add(Mode.PAPER, "breakout", -0.5, i)
        add(Mode.LIVE, "momentum", 0.2 if i % 2 else -0.3, i)
    sa = StrategyAnalytics(system.repo)
    st = sa.per_sub_strategy(Mode.PAPER)
    assert st["crypto_momentum.momentum"]["net_pnl"] == pytest.approx(40)
    assert st["crypto_momentum.breakout"]["net_pnl"] == pytest.approx(-20)
    assert st["crypto_momentum"]["trades"] == 80
    rv = sa.real_vs_virtual("crypto_momentum")
    assert rv["paper_virtual"]["net_pnl"] == pytest.approx(20) and rv["live_real"]["net_pnl"] == pytest.approx(-2)
    assert rv["flags"]  # live much worse than paper -> flagged


def test_strategy_correlation_and_degradation_report(system):
    from trinity.ai.performance_analyzer import StrategyAnalytics

    t0 = system.clock.now()
    for d in range(15):
        for sid, sign in (("crypto_momentum", 1), ("stock_trend", 1)):
            system.repo.upsert_trade_fill(strategy_id=sid, venue="v", symbol="X", mode=Mode.PAPER, quantity=1,
                                          entry_price=1, exit_price=1, entry_time=t0, fees_usd=0,
                                          exit_time=t0 + timedelta(days=d), realized_pnl=sign * (d % 4 - 1.5),
                                          exit_reason="", exit_client_order_id=f"{sid}{d}", is_virtual=True)
    sa = StrategyAnalytics(system.repo)
    corr = sa.strategy_correlations(Mode.PAPER)
    assert corr["crypto_momentum~stock_trend"] == pytest.approx(1.0)
    for i in range(40):  # older trades win, recent trades lose -> degradation
        system.repo.upsert_trade_fill(strategy_id="stock_trend", venue="v", symbol="X", mode=Mode.PAPER, quantity=1,
                                      entry_price=1, exit_price=1, entry_time=t0, fees_usd=0, exit_time=t0,
                                      realized_pnl=1.0 if i < 20 else -1.0, exit_reason="",
                                      exit_client_order_id=f"deg{i}", is_virtual=True, sub_strategy="trend")
    rep = sa.degradation_report(Mode.PAPER)
    assert "stock_trend.trend" in rep
