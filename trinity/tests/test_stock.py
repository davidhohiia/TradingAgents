"""Stock Trend: calendar, signals, entry/exit logic, bot sessions, Alpaca adapter (mocked HTTP)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import numpy as np
import pandas as pd
import pytest

from trinity.bots.stock_trend.bot import StockTrendBot
from trinity.bots.stock_trend.signals import compute_signals
from trinity.bots.stock_trend.strategy import PositionState, StockTrendStrategy
from trinity.core.market_calendar import UsEquityCalendar
from trinity.core.models import Mode, OrderStatus, OrderType, Side
from trinity.core.order_gateway import OrderGateway
from trinity.exchanges.base_exchange import AuthError, RateLimited
from trinity.exchanges.stock_broker import AlpacaBroker
from trinity.tests.conftest import build_system


def daily_df(closes, volume=80_000_000.0):
    idx = pd.date_range("2025-01-01", periods=len(closes), freq="D", tz="UTC")
    closes = np.asarray(closes, dtype=float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    vol = np.where(np.diff(np.concatenate([[closes[0]], closes])) > 0, volume * 1.3, volume * 0.7)
    return pd.DataFrame({"open": opens, "high": np.maximum(opens, closes) * 1.004,
                         "low": np.minimum(opens, closes) * 0.996, "close": closes, "volume": vol}, index=idx)


def uptrend(n=120, start=400.0):
    rng = np.random.default_rng(1)
    return start * np.cumprod(1 + 0.002 + rng.normal(0, 0.006, n))


def market(config, symbol="SPY", fractional=True):
    return config.instruments[symbol].to_market_info(symbol, fractional=fractional)


# ---------------------------------------------------------------- calendar
def test_calendar_sessions(config):
    cal = UsEquityCalendar(config.session)
    assert cal.is_open(datetime(2026, 3, 2, 15, 0, tzinfo=UTC))  # Mon 10:00 NY
    assert not cal.is_open(datetime(2026, 3, 2, 13, 0, tzinfo=UTC))  # 08:00 NY
    assert not cal.is_open(datetime(2026, 3, 7, 16, 0, tzinfo=UTC))  # Saturday
    assert not cal.is_open(datetime(2026, 12, 25, 16, 0, tzinfo=UTC))  # holiday
    assert not cal.is_open(datetime(2026, 11, 27, 19, 0, tzinfo=UTC))  # early close 13:00 NY
    assert cal.is_open(datetime(2026, 11, 27, 17, 0, tzinfo=UTC))
    nxt = cal.next_open(datetime(2026, 3, 6, 22, 0, tzinfo=UTC))  # Friday evening
    assert nxt.weekday() == 0 and nxt.hour == 9 and nxt.minute == 30


# ---------------------------------------------------------------- signals / entry
def test_entry_in_confirmed_uptrend(config):
    cfg = config.strategies.stock_trend.model_copy(update={"rsi_max": 90})
    sig = compute_signals(daily_df(uptrend()), cfg)
    assert sig.complete and sig.trend_up
    d = StockTrendStrategy(cfg).evaluate_entry(
        sig, bid=sig.close, ask=sig.close * 1.0001, market=market(config), strategy_capital=150, risk_pct=0.01,
        available_cash=150, max_order_notional=75, max_position_notional=90)
    assert d.enter, d.reasons
    assert d.stop < d.entry_price < d.take_profit
    assert d.sizing.risk_usd <= 1.5 + 1e-9 and d.sizing.notional <= 75 + 1e-9


def test_no_entry_in_downtrend_or_bad_rsi(config):
    cfg = config.strategies.stock_trend
    down = uptrend()[::-1]
    sig = compute_signals(daily_df(down), cfg)
    d = StockTrendStrategy(cfg).evaluate_entry(sig, bid=sig.close, ask=sig.close * 1.0001, market=market(config),
                                               strategy_capital=150, risk_pct=0.01, available_cash=150,
                                               max_order_notional=75, max_position_notional=90)
    assert not d.enter
    cfg2 = cfg.model_copy(update={"rsi_min": 10, "rsi_max": 20})
    sig2 = compute_signals(daily_df(uptrend()), cfg2)
    d2 = StockTrendStrategy(cfg2).evaluate_entry(sig2, bid=sig2.close, ask=sig2.close, market=market(config),
                                                 strategy_capital=150, risk_pct=0.01, available_cash=150,
                                                 max_order_notional=75, max_position_notional=90)
    assert not d2.enter and any("RSI" in r for r in d2.reasons)


def test_no_entry_without_liquidity_or_insufficient_history(config):
    cfg = config.strategies.stock_trend.model_copy(update={"rsi_max": 90})
    sig = compute_signals(daily_df(uptrend(), volume=1000), cfg)
    d = StockTrendStrategy(cfg).evaluate_entry(sig, bid=sig.close, ask=sig.close, market=market(config),
                                               strategy_capital=150, risk_pct=0.01, available_cash=150,
                                               max_order_notional=75, max_position_notional=90)
    assert not d.enter and any("liquidity" in r for r in d.reasons)
    short = compute_signals(daily_df(uptrend(20)), cfg)
    assert not short.complete


def test_whole_shares_only_rejects_when_unaffordable(config):
    cfg = config.strategies.stock_trend.model_copy(update={"rsi_max": 90})
    sig = compute_signals(daily_df(uptrend(start=600)), cfg)
    d = StockTrendStrategy(cfg).evaluate_entry(sig, bid=sig.close, ask=sig.close, market=market(config, fractional=False),
                                               strategy_capital=150, risk_pct=0.01, available_cash=150,
                                               max_order_notional=75, max_position_notional=90)
    assert not d.enter and any("whole shares" in r for r in d.reasons)


def test_exit_rules(config):
    cfg = config.strategies.stock_trend
    s = StockTrendStrategy(cfg)
    sig = compute_signals(daily_df(uptrend()), cfg)
    sig.close, sig.atr = 105.0, 1.0  # bring the signal to the position's price scale
    sig.ema_fast, sig.ema_slow = 104.0, 102.0
    ps = PositionState(entry_price=100, initial_stop=95, take_profit=112.5, highest_close=100)
    assert s.evaluate_exit(sig, ps, bid=94.9).protective
    assert "take profit" in s.evaluate_exit(sig, PositionState(100, 95, 112.5, 100), bid=113).reason
    sig.close, sig.atr = 110.0, 1.0
    ps2 = PositionState(100, 95, 200, 100)
    d = s.evaluate_exit(sig, ps2, bid=109)
    assert not d.exit and ps2.trailing_stop == pytest.approx(107)  # 110 - 3*ATR
    assert "trailing" in s.evaluate_exit(sig, ps2, bid=106.9).reason
    sig_rev = compute_signals(daily_df(uptrend()[::-1]), cfg)
    assert "reversal" in s.evaluate_exit(sig_rev, PositionState(400, 300, 1000, 400), bid=400).reason


# ---------------------------------------------------------------- bot with paper broker
def make_bot(system, config):
    gw = OrderGateway(system.engine, "stock_trend", "stocks")
    cal = UsEquityCalendar(config.session)
    cfg = config.strategies.stock_trend.model_copy(update={"rsi_max": 95, "min_up_down_volume_ratio": 0.0})
    return StockTrendBot(cfg, config, cal, gateway=gw, market=system.stocks, repo=system.repo, mode=Mode.PAPER,
                         clock=system.clock)


async def test_bot_no_orders_outside_session(tmp_path, config):
    from trinity.core.clock import SimClock

    s = build_system(tmp_path, config, clock=SimClock(datetime(2026, 3, 7, 16, 0, tzinfo=UTC)))  # Saturday
    bot = make_bot(s, config)
    await bot.start()
    for _ in range(10):
        await bot.safe_step()
        s.clock.advance(3600)
    assert s.repo.list_orders(Mode.PAPER) == []
    assert bot.info["session_open"] is False


async def test_bot_trades_in_session_and_exits_on_stop(system, config):
    bot = make_bot(system, config)
    await bot.start()
    for _ in range(5):
        assert await bot.safe_step(), bot.last_error
        system.clock.advance(60)
    buys = [o for o in system.repo.list_orders(Mode.PAPER) if o.side == Side.BUY]
    assert buys, bot.info
    assert all(o.order_type == OrderType.LIMIT and o.stop_price and o.take_profit for o in buys)
    pos = [p for p in system.positions.all(open_only=True) if p.strategy_id == "stock_trend"]
    assert pos
    sym = pos[0].symbol
    # crash the price below the stop -> protective market exit during the session
    system.stock_market.set_price(sym, pos[0].stop_price * 0.97)
    system.clock.advance(60)
    await bot.safe_step()
    sells = [o for o in system.repo.list_orders(Mode.PAPER) if o.side == Side.SELL and o.symbol == sym]
    assert sells and sells[0].status == OrderStatus.FILLED and "stop" in sells[0].reason
    assert not system.positions.get("stock_trend", "stocks", sym).is_open


# ---------------------------------------------------------------- Alpaca adapter (mocked HTTP, no network)
def alpaca_transport(account=None, assets=None, status_override=None):
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if status_override:
            return httpx.Response(status_override, json={"message": "x"})
        path = req.url.path
        if path == "/v2/account":
            return httpx.Response(200, json=account or {"status": "ACTIVE", "cash": "150", "currency": "USD",
                                                        "multiplier": "1", "trading_blocked": False})
        if path.startswith("/v2/assets/"):
            sym = path.rsplit("/", 1)[1]
            if assets and sym in assets:
                return httpx.Response(200, json=assets[sym])
            return httpx.Response(404, json={"message": "not found"})
        if path == "/v2/orders" and req.method == "POST":
            body = json.loads(req.content)
            return httpx.Response(200, json={"id": "abc", "client_order_id": body["client_order_id"],
                                             "symbol": body["symbol"], "side": body["side"], "type": body["type"],
                                             "qty": body["qty"], "filled_qty": "0", "status": "accepted",
                                             "limit_price": body.get("limit_price")})
        if path == "/v2/clock":
            return httpx.Response(200, json={"is_open": True})
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler), calls


async def test_alpaca_capabilities_and_order_mapping(config):
    from trinity.core.models import OrderRequest

    assets = {"SPY": {"tradable": True, "fractionable": True, "status": "active"},
              "QQQ": {"tradable": True, "fractionable": False, "status": "active"}}
    transport, calls = alpaca_transport(assets=assets)
    fb = {s: config.instruments[s].to_market_info(s) for s in ("SPY", "QQQ", "VTI")}
    b = AlpacaBroker("KEYID12345", "SECRET12345", instruments_fallback=fb, transport=transport,
                     data=object())  # type: ignore[arg-type]
    rep = await b.verify_capabilities(["SPY", "QQQ", "VTI"])
    assert rep.api_reachable and rep.trading_allowed and rep.cash_account
    assert rep.instruments["SPY"]["fractionable"] and not rep.instruments["QQQ"]["fractionable"]
    assert any("VTI" in p for p in rep.problems)  # unknown at broker -> not assumed available
    assert b.market_info("QQQ").qty_step == 1.0 and not b.market_info("QQQ").fractional
    o = await b.create_order(OrderRequest("trstoc-1", "SPY", Side.BUY, OrderType.LIMIT, 0.1234, 500.123))
    assert o.status == OrderStatus.OPEN and o.client_order_id == "trstoc-1"
    body = json.loads(calls[-1].content)
    assert body["limit_price"] == "500.12" and body["time_in_force"] == "day"
    assert calls[-1].headers["APCA-API-KEY-ID"] == "KEYID12345"
    assert calls[-1].url.host == "paper-api.alpaca.markets"


async def test_alpaca_margin_account_flagged_and_errors_mapped(config):
    transport, _ = alpaca_transport(account={"status": "ACTIVE", "multiplier": "2", "cash": "100"})
    b = AlpacaBroker("K1234567", "S1234567", transport=transport, data=object())  # type: ignore[arg-type]
    rep = await b.verify_capabilities([])
    assert not rep.cash_account and not rep.ok
    t401, _ = alpaca_transport(status_override=401)
    with pytest.raises(AuthError):
        await AlpacaBroker("K1234567", "S1234567", transport=t401, data=object()).fetch_balances()  # type: ignore[arg-type]
    t429, _ = alpaca_transport(status_override=429)
    b429 = AlpacaBroker("K1234567", "S1234567", transport=t429, data=object())  # type: ignore[arg-type]
    import trinity.exchanges.base_exchange as be

    orig = be.asyncio.sleep

    async def fast(_):
        return None

    be.asyncio.sleep = fast
    try:
        with pytest.raises(RateLimited):
            await b429.fetch_balances()
    finally:
        be.asyncio.sleep = orig


def test_alpaca_requires_keys():
    from trinity.exchanges.base_exchange import NotConfigured

    with pytest.raises(NotConfigured):
        AlpacaBroker(None, None)


def test_stale_completed_bar_filter_logic():
    now = datetime(2026, 3, 2, 15, tzinfo=UTC)
    bars = [now - timedelta(days=1), now.replace(hour=5)]
    assert [b for b in bars if b.date() < now.date()] == [bars[0]]


# ---------------------------------------------------------------- breakout detection
def test_stock_breakout_detection(config):
    from trinity.bots.stock_trend.breakout import detect_breakout, evaluate_breakout_entry

    cfg = config.strategies.stock_trend
    closes = list(uptrend(120))
    base = closes[-1]
    closes += [base * (1 + 0.003 * np.sin(i)) for i in range(60)]
    closes.append(base * 1.03)  # close above the 55-day high
    df = daily_df(closes)
    df.iloc[-1, df.columns.get_loc("volume")] = df["volume"].iloc[-30:-1].mean() * 2
    level, why = detect_breakout(df, cfg)
    assert level is not None, why
    sig = compute_signals(df, cfg)
    m = market(config)
    d = evaluate_breakout_entry(df, sig, cfg.model_copy(update={"breakout": cfg.breakout.model_copy(
        update={"max_extension_atr": 5})}), bid=sig.close, ask=sig.close * 1.0001, market=m, strategy_capital=3.75,
        risk_pct=1.0, available_cash=100, max_order_notional=120, max_position_notional=60)
    assert d.enter, d.reasons
    assert d.stop < level < d.entry_price and d.sizing.risk_usd <= 3.75 + 1e-9
    df.iloc[-1, df.columns.get_loc("volume")] = 1.0
    assert detect_breakout(df, cfg)[0] is None  # no volume confirmation
