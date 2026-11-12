"""Backtest adapters that reuse the SAME decision code as the live bots."""

from __future__ import annotations

from datetime import UTC

from trinity.backtesting.engine import BarContext, BtOrder
from trinity.bots.crypto_momentum.planner import MomentumPlanner
from trinity.bots.crypto_momentum.strategies.base import TradeState
from trinity.bots.stock_trend.signals import compute_signals
from trinity.bots.stock_trend.strategy import PositionState, StockTrendStrategy
from trinity.config.settings import CryptoMomentumConfig, StockTrendConfig
from trinity.core.models import (
    MarketInfo,
    OrderBook,
)


def _book_from_close(close: float, ts, spread_bps: float) -> OrderBook:  # noqa: ANN001
    hs = close * spread_bps / 20_000
    ts = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return OrderBook("BT", [(close - hs, 1e12)], [(close + hs, 1e12)], ts, "backtest", synthetic=True)


class MomentumBacktest:
    """Crypto Momentum (momentum + confirmed breakout) on one symbol, using the bot's MomentumPlanner."""

    def __init__(self, cfg: CryptoMomentumConfig, market: MarketInfo, max_risk_usd: float,
                 max_order_notional: float, spread_bps: float = 2.0) -> None:
        self.planner = MomentumPlanner(cfg)
        self.market = market
        self.max_risk = max_risk_usd
        self.max_order = max_order_notional
        self.spread_bps = spread_bps
        self.state: TradeState | None = None
        self.pending: tuple[str, float, float, float, float, float | None] | None = None
        self.cooldown_until = -1
        self.cooldown_bars = cfg.cooldown_bars_after_exit
        self.trades_by_strategy: dict[str, int] = {"momentum": 0, "breakout": 0}

    def on_bar(self, ctx: BarContext) -> tuple[list[BtOrder], list[int]]:
        for f in ctx.fills_last_bar:
            if f.side == "buy" and self.pending is not None:
                name, stop, tp, trail, act, level = self.pending
                self.state = TradeState(name, ctx.avg_cost, stop, tp, f.price, trail, act, level)
                self.trades_by_strategy[name] += 1
                self.pending = None
        if ctx.position <= 0 and self.state is not None:
            self.state = None
            self.cooldown_until = ctx.index + self.cooldown_bars
        cancel = [o.id for o in ctx.open_orders]  # entries/stops are re-issued every bar
        feat = self.planner.features(ctx.history)
        if not self.planner.ready(feat):
            return [], cancel
        close = float(feat["close"].iloc[-1])
        hs = close * self.spread_bps / 20_000
        if ctx.position > 0:
            if self.state is None:  # should not happen; protect anyway
                self.state = TradeState("momentum", ctx.avg_cost, ctx.avg_cost * 0.95, ctx.avg_cost * 1.1, close,
                                        2.5, 1.0)
            d = self.planner.exit_decision(feat, self.state, close - hs)
            if d.exit:
                return [BtOrder("sell", "market", ctx.position, tag="exit")], cancel
            return [BtOrder("sell", "stop", ctx.position, self.state.active_stop, tag="stop"),
                    BtOrder("sell", "limit", ctx.position, self.state.take_profit, tag="tp")], cancel
        if ctx.index < self.cooldown_until:
            return [], cancel
        cands, _ = self.planner.candidates({"X": feat}, set())
        if not cands:
            return [], cancel
        sig = cands[0]
        price = close + hs
        qty = self.planner.size(sig, max_risk_usd=self.max_risk, available=ctx.cash, max_notional=self.max_order,
                                market=self.market, price=price)
        if qty <= 0:
            return [], cancel
        stop = price - sig.risk_per_unit
        self.pending = (sig.strategy, stop, price + (sig.take_profit - sig.entry), sig.trailing_atr_multiplier,
                        sig.trailing_activation_r, sig.level)
        return [BtOrder("buy", "market", qty, tag=f"entry:{sig.strategy}")], cancel


class TrendBacktest:
    def __init__(self, cfg: StockTrendConfig, market: MarketInfo, capital: float, risk_pct: float,
                 max_order_notional: float, spread_bps: float = 2.0) -> None:
        self.cfg = cfg
        self.strategy = StockTrendStrategy(cfg)
        self.market = market
        self.capital = capital
        self.risk_pct = risk_pct
        self.max_order = max_order_notional
        self.spread_bps = spread_bps
        self.ps: PositionState | None = None
        self.pending_entry: tuple[float, float] | None = None  # (stop, tp)

    def on_bar(self, ctx: BarContext) -> tuple[list[BtOrder], list[int]]:
        for f in ctx.fills_last_bar:
            if f.side == "buy" and self.pending_entry:
                stop, tp = self.pending_entry
                self.ps = PositionState(ctx.avg_cost, stop, tp, f.price)
                self.pending_entry = None
        if ctx.position <= 0:
            self.ps = None
        sig = compute_signals(ctx.history, self.cfg)
        close = float(ctx.history["close"].iloc[-1])
        hs = close * self.spread_bps / 20_000
        cancel = [o.id for o in ctx.open_orders]  # re-issue orders each bar (limits are day orders)
        new: list[BtOrder] = []
        if ctx.position > 0:
            if self.ps is None:
                self.ps = PositionState(ctx.avg_cost, ctx.avg_cost * 0.95, ctx.avg_cost * 1.1, close)
            dec = self.strategy.evaluate_exit(sig, self.ps, bid=close - hs)
            if dec.exit:
                new.append(BtOrder("sell", "market", ctx.position, tag="exit"))
            else:
                stop = max(self.ps.initial_stop, self.ps.trailing_stop or 0.0)
                new.append(BtOrder("sell", "stop", ctx.position, stop, tag="stop"))
                new.append(BtOrder("sell", "limit", ctx.position, self.ps.take_profit, tag="tp"))
            return new, cancel
        dec = self.strategy.evaluate_entry(
            sig, bid=close - hs, ask=close + hs, market=self.market, strategy_capital=self.capital,
            risk_pct=self.risk_pct, available_cash=ctx.cash, max_order_notional=self.max_order,
            max_position_notional=self.capital * 0.6)
        if dec.enter and dec.sizing and dec.entry_price:
            self.pending_entry = (dec.stop, dec.take_profit)  # type: ignore[assignment]
            new.append(BtOrder("buy", "limit", dec.sizing.quantity, dec.entry_price, tag="entry"))
        return new, cancel


def _iso(ts) -> str:  # noqa: ANN001
    return ts.isoformat()
