"""Stock Trend AI bot: EMA20/50 trend following on ETFs (SPY, QQQ, VTI).

* trades only inside the regular session (broker clock / exchange calendar);
* long only, no leverage, no margin, no options/CFD;
* fractional shares only if broker AND asset support them;
* stops are monitored by the bot (synthetic); between sessions gaps can exceed the stop.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from trinity.ai.anomaly_detector import AnomalyDetector
from trinity.bots.base import BaseBot
from trinity.bots.stock_trend.signals import compute_signals
from trinity.bots.stock_trend.strategy import PositionState, StockTrendStrategy
from trinity.config.settings import AppConfig, StockTrendConfig
from trinity.core.indicators import candles_to_df, floor_to_step
from trinity.core.market_calendar import UsEquityCalendar
from trinity.core.models import AssetClass, Fill, OrderIntent, OrderRecord, OrderType, Side

ENTRY_ORDER_TTL = timedelta(minutes=15)


class StockTrendBot(BaseBot):
    bot_type = "stock_trend"

    def __init__(self, cfg: StockTrendConfig, app_config: AppConfig, calendar: UsEquityCalendar,
                 anomaly: AnomalyDetector | None = None, **kwargs: Any) -> None:
        super().__init__(strategy_id=cfg.strategy_id, loop_interval=cfg.loop_interval_seconds, **kwargs)
        self.cfg = cfg
        self.app = app_config
        self.calendar = calendar
        self.strategy = StockTrendStrategy(cfg)
        self.anomaly = anomaly or AnomalyDetector(max_spread_pct=0.005)
        raw = self.load_state()
        self.pos_state: dict[str, PositionState] = {
            s: PositionState(**v) for s, v in raw.get("positions", {}).items()
        }
        self.last_exit_day: dict[str, str] = raw.get("last_exit_day", {})
        if self.gateway is not None:
            self.gateway.on_fill(self._on_fill)

    def _persist(self) -> None:
        self.save_state({"positions": {s: asdict(p) for s, p in self.pos_state.items()},
                         "last_exit_day": self.last_exit_day})

    async def _on_fill(self, order: OrderRecord, fill: Fill) -> None:
        assert self.gateway is not None
        if order.side == Side.BUY:
            pos = self.gateway.position(order.symbol)
            ps = self.pos_state.get(order.symbol)
            if ps is None and order.stop_price and order.take_profit:
                self.pos_state[order.symbol] = PositionState(
                    entry_price=pos.avg_entry_price, initial_stop=order.stop_price, take_profit=order.take_profit,
                    highest_close=fill.price)
            elif ps is not None:
                ps.entry_price = pos.avg_entry_price
            self.gateway._engine.positions.set_protection(  # noqa: SLF001
                self.strategy_id, self.gateway.venue, order.symbol, order.stop_price, order.take_profit)
            kind = "position_opened"
        else:
            if not self.gateway.position(order.symbol).is_open:
                self.pos_state.pop(order.symbol, None)
                self.last_exit_day[order.symbol] = self.clock.now().date().isoformat()
            kind = "stop_loss" if "stop" in order.reason else (
                "take_profit" if "take profit" in order.reason else "position_closed")
        self._persist()
        await self.notifier.notify(
            kind, f"[{self.mode.value.upper()}] STOCK {order.side.value.upper()} {fill.quantity:g} {order.symbol} "
                  f"@ {fill.price:.2f}" + (f" PnL ${fill.realized_pnl:+.2f}" if order.side == Side.SELL else "")
                  + (f" ({order.reason})" if order.reason else ""))

    async def step(self) -> None:
        assert self.gateway is not None and self.market is not None
        now = self.clock.now()
        await self.gateway.sync()
        try:
            session_open = await self.market.is_market_open(now)
        except Exception:  # noqa: BLE001 - broker clock unavailable -> exchange calendar fallback
            session_open = self.calendar.is_open(now)
        session_open = session_open and self.calendar.is_open(now)
        self.info = {"session_open": session_open, "symbols": {}}
        if not session_open:
            self.info["next_open"] = self.calendar.next_open(now).isoformat()
            return
        # cancel stale unfilled entries
        for o in self.gateway.active_orders():
            if o.side == Side.BUY and now - o.created_at > ENTRY_ORDER_TTL:
                await self.gateway.cancel(o.client_order_id, "entry order expired")
        locks = self.gateway.risk_locked()
        cap = self.gateway.capital()
        open_syms = [s for s in self.cfg.symbols if self.gateway.position(s).is_open]
        engine = self.gateway._engine  # noqa: SLF001
        for symbol in self.cfg.symbols:
            info: dict[str, Any] = {}
            self.info["symbols"][symbol] = info
            candles = await self.market.fetch_ohlcv(symbol, self.cfg.timeframe, self.cfg.history_bars)
            book = await self.market.fetch_order_book(symbol, 5)
            issues = self.anomaly.check_book(book, now, max_age_s=self.app.risk.price_age_limit(AssetClass.STOCK))
            if issues:
                info["skipped"] = issues
                continue
            # decisions use COMPLETED daily bars only (today's partial bar is excluded)
            completed = [c for c in candles if c.ts.date() < now.date()]
            df = candles_to_df(completed)
            sig = compute_signals(df, self.cfg)
            bid, ask = book.best_bid or 0.0, book.best_ask or 0.0
            info.update({"close": sig.close, "ema_fast": sig.ema_fast, "ema_slow": sig.ema_slow, "rsi": sig.rsi,
                         "atr": sig.atr, "bid": bid, "ask": ask})
            market = engine.venues[self.gateway.venue].market_info(symbol)
            pos = self.gateway.position(symbol)
            if pos.is_open:
                ps = self.pos_state.get(symbol)
                if ps is None:  # recovered after restart without state: conservative default stop
                    stop = pos.stop_price or pos.avg_entry_price * 0.95
                    ps = PositionState(pos.avg_entry_price, stop, pos.take_profit or pos.avg_entry_price * 1.1,
                                       pos.avg_entry_price)
                    self.pos_state[symbol] = ps
                dec = self.strategy.evaluate_exit(sig, ps, bid=bid)
                info["trailing_stop"] = ps.trailing_stop
                if dec.exit and not any(o.side == Side.SELL for o in self.gateway.active_orders(symbol)):
                    qty = floor_to_step(pos.quantity, market.qty_step) if market.fractional else pos.quantity
                    res = await self.gateway.submit(OrderIntent(
                        strategy_id=self.strategy_id, venue=self.gateway.venue, symbol=symbol, side=Side.SELL,
                        order_type=OrderType.MARKET, quantity=qty, reference_price=bid, market_data_ts=book.ts,
                        asset_class=AssetClass.STOCK, reduce_only=True, protective_exit=dec.protective,
                        reason=dec.reason))
                    info["exit"] = {"reason": dec.reason, "accepted": res.accepted, "why": res.reasons}
                continue
            if locks or self.paused:
                info["entry"] = f"blocked by risk locks {locks}"
                continue
            if self.last_exit_day.get(symbol) == now.date().isoformat():
                info["entry"] = "no re-entry on the day of an exit"
                continue
            if len(open_syms) >= self.cfg.max_positions:
                info["entry"] = "max positions reached"
                continue
            if self.gateway.active_orders(symbol):
                info["entry"] = "entry order pending"
                continue
            inst = self.app.instruments[symbol]
            dec = self.strategy.evaluate_entry(
                sig, bid=bid, ask=ask, market=market, strategy_capital=cap.effective_capital,
                risk_pct=self.gateway.max_risk_per_trade_pct, available_cash=cap.available,
                max_order_notional=self.app.risk.max_order_notional_usd,
                max_position_notional=self.app.risk.max_position_pct_of_strategy * cap.effective_capital,
                sell_fee_pct=inst.sell_regulatory_fee_pct,
            )
            info["entry"] = dec.reasons
            if not dec.enter or dec.sizing is None:
                continue
            res = await self.gateway.submit(OrderIntent(
                strategy_id=self.strategy_id, venue=self.gateway.venue, symbol=symbol, side=Side.BUY,
                order_type=OrderType.LIMIT, quantity=dec.sizing.quantity, reference_price=ask,
                market_data_ts=book.ts, asset_class=AssetClass.STOCK, price=dec.entry_price,
                stop_price=dec.stop, take_profit=dec.take_profit, reason="; ".join(dec.reasons)))
            info["submitted"] = {"accepted": res.accepted, "why": res.reasons}
            if res.accepted:
                open_syms.append(symbol)
        self._persist()


def _iso(ts: datetime) -> str:
    return ts.isoformat()
