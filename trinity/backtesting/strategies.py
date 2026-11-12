"""Backtest adapters that reuse the SAME decision code as the live bots."""

from __future__ import annotations

from datetime import UTC

from trinity.backtesting.engine import BarContext, BtOrder
from trinity.bots.crypto_grid.strategy import GridStrategy, GridSymbolState, Lot
from trinity.bots.stock_trend.signals import compute_signals
from trinity.bots.stock_trend.strategy import PositionState, StockTrendStrategy
from trinity.config.settings import GridStrategyConfig, StockTrendConfig
from trinity.core.models import (
    MarketInfo,
    Mode,
    OrderBook,
    OrderRecord,
    OrderStatus,
    OrderType,
    Side,
)


def _book_from_close(close: float, ts, spread_bps: float) -> OrderBook:  # noqa: ANN001
    hs = close * spread_bps / 20_000
    ts = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return OrderBook("BT", [(close - hs, 1e12)], [(close + hs, 1e12)], ts, "backtest", synthetic=True)


class GridBacktest:
    """Runs GridStrategy.plan() bar by bar. Order ids are assigned by the engine after on_bar
    returns, so sells are bound to their lots at the start of the next bar."""

    def __init__(self, cfg: GridStrategyConfig, market: MarketInfo, capital: float, risk_pct: float,
                 spread_bps: float = 2.0) -> None:
        self.strategy = GridStrategy(cfg)
        self.market = market
        self.max_risk = capital * risk_pct
        self.spread_bps = spread_bps
        self.st = GridSymbolState()
        self._pending_bind: list[tuple[BtOrder, list[str]]] = []
        self._new_buys: list[tuple[BtOrder, float]] = []
        self._last_ts = None

    def _apply_fills(self, ctx: BarContext) -> None:
        for o, cids in self._pending_bind:
            for lot in self.st.lots:
                if lot.buy_cid in cids:
                    lot.sell_cid = str(o.id)
        self._pending_bind = []
        for o, level in self._new_buys:
            if o.id:
                self.st.buy_orders[str(o.id)] = level
                self.st.buy_created[str(o.id)] = _iso(self._last_ts)
        self._new_buys = []
        for f in ctx.fills_last_bar:
            if f.side == "buy":
                lot = next((lot_ for lot_ in self.st.lots if lot_.buy_cid == str(f.order_id)), None)
                if lot is None:
                    lot = Lot(str(f.order_id), 0.0, f.price)
                    self.st.lots.append(lot)
                lot.qty += f.qty
            elif f.tag == "stop":
                self.st.lots.clear()
            else:
                remaining = f.qty
                for lot in [lot_ for lot_ in self.st.lots if lot_.sell_cid == str(f.order_id)]:
                    take = min(lot.qty, remaining)
                    lot.qty -= take
                    remaining -= take
        excess = sum(lot_.qty for lot_ in self.st.lots) - ctx.position
        if excess > 1e-12:
            for lot in reversed(self.st.lots):
                take = min(lot.qty, excess)
                lot.qty -= take
                excess -= take
        self.st.lots = [lot_ for lot_ in self.st.lots if lot_.qty > 1e-12]

    def on_bar(self, ctx: BarContext) -> tuple[list[BtOrder], list[int]]:
        self._apply_fills(ctx)
        close = float(ctx.history["close"].iloc[-1])
        book = _book_from_close(close, ctx.ts, self.spread_bps)
        self._last_ts = book.ts
        active = [
            OrderRecord(str(o.id), "bt", "bt", "BT", Mode.PAPER, Side(o.side),
                        OrderType.LIMIT if o.kind == "limit" else OrderType.MARKET, o.qty, OrderStatus.OPEN,
                        price=o.price, filled_quantity=o.filled)
            for o in ctx.open_orders
        ]
        plan = self.strategy.plan(df=ctx.history, book=book, now=book.ts, st=self.st, position_qty=ctx.position,
                                  active=active, market=self.market, max_risk_usd=self.max_risk)
        new: list[BtOrder] = []
        for spec in plan.orders:
            kind = "market" if spec.order_type == OrderType.MARKET else "limit"
            o = BtOrder(spec.side.value, kind, spec.qty, spec.price, tag="stop" if spec.protective else "")
            new.append(o)
            if spec.side == Side.SELL:
                self._pending_bind.append((o, list(spec.lot_buy_cids)))
        # buy orders are tracked by the strategy state via their (engine-assigned) ids next bar
        self._new_buys = [(o, spec.level) for o, spec in zip(new, plan.orders, strict=True)
                          if spec.side == Side.BUY and spec.level is not None]
        return new, [int(c) for c in dict.fromkeys(plan.cancel)]


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
