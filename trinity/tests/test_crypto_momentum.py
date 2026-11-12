"""BOT 01 Crypto Momentum: indicators, both sub-strategies, correlation filter, exits, bot behaviour."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pytest

from trinity.bots.crypto_momentum.bot import CryptoMomentumBot, completed
from trinity.bots.crypto_momentum.planner import MomentumPlanner
from trinity.bots.crypto_momentum.signals import compute_features, volatility_filter
from trinity.bots.crypto_momentum.strategies.base import EntrySignal, TradeState, manage_exit
from trinity.bots.crypto_momentum.strategies.breakout import BreakoutStrategy
from trinity.bots.crypto_momentum.strategies.momentum import MomentumStrategy
from trinity.core.models import Candle, Mode, OrderType, Side
from trinity.core.order_gateway import OrderGateway
from trinity.tests.helpers import T0, crash, make_df, ranging, uptrend


def cfg(config):
    return config.strategies.crypto_momentum


def momentum_df():
    """Clean uptrend with rising volume on the last bars (artificial)."""
    prices = uptrend(320, drift=0.0015, noise=0.002, seed=3)
    prices[-1] = prices[-2] * 1.012  # acceleration on the last bar -> MACD histogram rising
    vol = [100.0] * 319 + [180.0]
    return make_df(prices, spread=0.004, volume=vol)


def breakout_df(confirm=True):
    """Range for 40 bars after a long uptrend, then a strong-volume close above the range high."""
    up = uptrend(260, drift=0.0012, noise=0.002, seed=4)
    base = up[-1]
    rng = [base * (1 + 0.004 * np.sin(i)) for i in range(40)]
    level = max(r * 1.004 for r in rng)  # make_df highs = max(open, close) * 1.004
    brk = level * 1.006
    tail = [brk, brk * 1.002] if confirm else [brk, level * 0.99]
    prices = up + rng + tail
    vol = [100.0] * (len(prices) - 2) + [400.0, 150.0]
    df = make_df(prices, spread=0.004, volume=vol)
    # breakout bar must close near its high
    i = len(df) - 2
    df.iloc[i, df.columns.get_loc("high")] = df["close"].iloc[i] * 1.001
    return df


def test_features_and_volatility_filter(config):
    feat = compute_features(momentum_df(), cfg(config).indicators)
    last = feat.iloc[-1]
    for col in ("ema_fast", "ema_mid", "ema_slow", "rsi", "macd", "macd_signal", "macd_hist", "atr", "vol_ratio"):
        assert last[col] == last[col], col
    ind = cfg(config).indicators
    assert not volatility_filter({"atr_pct": ind.min_atr_pct / 2}, ind)[0]
    assert not volatility_filter({"atr_pct": ind.max_atr_pct * 2}, ind)[0]
    assert volatility_filter({"atr_pct": (ind.min_atr_pct + ind.max_atr_pct) / 2}, ind)[0]


def test_momentum_entry_in_uptrend_only(config):
    c = cfg(config)
    s = MomentumStrategy(c.momentum.model_copy(update={"rsi_max": 95}), c.indicators)
    sig, why = s.entry("BTC/USDT", compute_features(momentum_df(), c.indicators))
    assert sig is not None, why
    assert sig.stop < sig.entry < sig.take_profit and sig.strategy == "momentum"
    assert sig.take_profit - sig.entry == pytest.approx(c.momentum.take_profit_r * (sig.entry - sig.stop))
    none, why = s.entry("BTC/USDT", compute_features(make_df(crash(320)), c.indicators))
    assert none is None and any("not aligned" in w for w in why)
    flat, why = s.entry("BTC/USDT", compute_features(make_df(ranging(320)), c.indicators))
    assert flat is None


def test_confirmed_breakout_requires_close_confirmation(config):
    c = cfg(config)
    s = BreakoutStrategy(c.breakout.model_copy(update={"max_extension_atr": 5}), c.indicators)
    sig, why = s.entry("ETH/USDT", compute_features(breakout_df(True), c.indicators))
    assert sig is not None, why
    assert sig.level is not None and sig.stop < sig.level < sig.entry
    failed, why = s.entry("ETH/USDT", compute_features(breakout_df(False), c.indicators))
    assert failed is None and any("not confirmed" in w for w in why)


def test_breakout_rejects_weak_volume(config):
    c = cfg(config)
    s = BreakoutStrategy(c.breakout.model_copy(update={"min_volume_ratio": 10, "max_extension_atr": 5}), c.indicators)
    sig, why = s.entry("ETH/USDT", compute_features(breakout_df(True), c.indicators))
    assert sig is None and any("volume" in w for w in why)


def test_strategies_are_separate_modules_with_own_exits(config):
    c = cfg(config)
    prices = uptrend(320, drift=0.0015, noise=0.002, seed=3)
    prices[-4:] = [prices[-5] * (1 - 0.015 * k) for k in range(1, 5)]  # sharp reversal
    feat = compute_features(make_df(prices), c.indicators)
    st_m = TradeState("momentum", 100, 90, 130, 100, 2.5, 1.0)
    st_b = TradeState("breakout", 100, 90, 130, 100, 2.5, 1.0, level=10**9)
    assert "momentum lost" in MomentumStrategy(c.momentum, c.indicators).exit_signal(feat, st_m)
    assert "failed breakout" in BreakoutStrategy(c.breakout, c.indicators).exit_signal(feat, st_b)


def test_manage_exit_trailing_never_lowers():
    st = TradeState("momentum", 100, 95, 120, 100, 2.0, 1.0)
    assert not manage_exit(st, 106, 1.0, 105.5).exit
    assert st.trailing_stop == pytest.approx(104)
    manage_exit(st, 103, 1.0, 104.5)
    assert st.trailing_stop == pytest.approx(104)  # not lowered after the pullback
    d = manage_exit(st, 103, 1.0, 103.9)
    assert d.exit and d.protective and "trailing" in d.reason
    assert manage_exit(TradeState("m", 100, 95, 110, 100, 2, 1), 100, 1, 94).reason.startswith("stop loss")
    assert "take profit" in manage_exit(TradeState("m", 100, 95, 110, 100, 2, 1), 111, 1, 111).reason


def test_correlation_filter_one_position_per_cluster(config):
    p = MomentumPlanner(cfg(config), 0.8)
    base = np.array(uptrend(300, seed=9))
    noise = np.random.default_rng(1).normal(0, 0.0005, 300)
    feats = {"BTC/USDT": make_df(list(base)), "ETH/USDT": make_df(list(base * 0.05 * (1 + noise))),
             "SOL/USDT": make_df(list(np.array(ranging(300, 150, 0.05)))),}
    feats = {k: compute_features(v, cfg(config).indicators) for k, v in feats.items()}
    matrix = p.correlations(feats)
    assert matrix[("BTC/USDT", "ETH/USDT")] > 0.9
    cands = [EntrySignal("momentum", "BTC/USDT", 100, 95, 110, 2.0), EntrySignal("momentum", "ETH/USDT", 100, 95, 110, 1.0),
             EntrySignal("breakout", "SOL/USDT", 100, 95, 110, 0.5)]
    from trinity.bots.crypto_momentum.planner import PlanNotes

    notes = PlanNotes()
    chosen = [c.symbol for c in p.select(cands, [], matrix, notes)]
    assert "BTC/USDT" in chosen and "ETH/USDT" not in chosen  # practically identical risk skipped
    assert any("ETH/USDT" in n and "correlated" in n for n in notes.correlation)
    notes2 = PlanNotes()
    assert "BTC/USDT" not in [c.symbol for c in p.select(cands, ["ETH/USDT"], matrix, notes2)]


def test_risk_based_sizing_and_leverage_does_not_raise_risk(config):
    m = config.instruments["BTC/USDT"].to_market_info("BTC/USDT")
    sig = EntrySignal("momentum", "BTC/USDT", 60000, 58800, 63600, 1)
    q1 = MomentumPlanner.size(sig, max_risk_usd=3.75, available=150, max_notional=120, market=m, price=60000)
    assert q1 * (sig.entry - sig.stop) <= 3.75 + 1e-9 and q1 * 60000 <= 120 + 1e-6
    q2 = MomentumPlanner.size(sig, max_risk_usd=3.75, available=40, max_notional=120, market=m, price=60000,
                              leverage=2.0)
    assert q2 * (sig.entry - sig.stop) <= 3.75 + 1e-9  # risk budget unchanged by leverage


def test_completed_bars_only():
    now = T0 + timedelta(hours=10, minutes=30)
    candles = [Candle(T0 + timedelta(hours=i), 1, 1, 1, 1, 1) for i in range(11)]
    assert completed(candles, now, "1h")[-1].ts == T0 + timedelta(hours=9)  # 10:00 bar still forming


# ---------------------------------------------------------------- bot on the paper exchange
def make_bot(system, config):
    gw = OrderGateway(system.engine, "crypto_momentum", "crypto")
    return CryptoMomentumBot(cfg(config), config, gateway=gw, market=system.crypto_market, repo=system.repo,
                             mode=Mode.PAPER, clock=system.clock)


async def test_bot_trades_with_stops_and_separate_sub_strategy_stats(tmp_path, config):
    from trinity.tests.conftest import build_system

    s = build_system(tmp_path, config, seed=21)
    for spec in s.crypto_market.specs.values():
        spec.annual_drift = 15.0  # strong synthetic uptrend -> momentum signals
        spec.annual_vol = 0.35
    s.crypto_market.__init__(s.crypto_market.name, s.crypto_market.asset_class, list(s.crypto_market.specs.values()),
                             s.clock, seed=21)
    bot = make_bot(s, config)
    await bot.start()
    for _ in range(48 * 6):
        assert await bot.safe_step(), bot.last_error
        s.clock.advance(600)
    orders = s.repo.list_orders(Mode.PAPER, limit=1000)
    buys = [o for o in orders if o.side == Side.BUY]
    assert buys, bot.info
    for o in buys:
        assert o.order_type == OrderType.LIMIT and o.stop_price and o.stop_price < o.price
        assert o.sub_strategy in ("momentum", "breakout")
        assert o.quantity * (o.price - o.stop_price) <= 0.0075 * 500 + 1e-6  # 0.75% of equity
    assert all(o.leverage == 1.0 for o in orders)  # never leverage by default
    held = {p.symbol for p in s.positions.all(open_only=True)}
    assert len(held) <= 3
    for p in s.positions.all(open_only=True):
        assert p.stop_price is not None and p.sub_strategy in ("momentum", "breakout")


async def test_bot_respects_locks_and_reports_outage(system, config):
    system.risk.pause("test")
    bot = make_bot(system, config)
    await bot.start()
    await bot.safe_step()
    assert not [o for o in system.repo.list_orders(Mode.PAPER) if o.side == Side.BUY]
    system.crypto_market.outages = [(system.clock.now(), system.clock.now() + timedelta(hours=1))]
    assert not await bot.safe_step()
    assert bot.status().value == "CONNECTION ERROR"
