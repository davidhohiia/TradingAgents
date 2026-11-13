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
from trinity.ai.quality_score import QualityInputs, QualityScore, score_trade
from trinity.bots.stock_trend.breakout import evaluate_breakout_entry
from trinity.config.settings import RegimeParams
from trinity.core.indicators import adx, last_valid, macd
from trinity.core.market_regime import classify_regime
from trinity.risk.correlation import return_correlations
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
        self._closes: dict[str, Any] = {}
        self._last_signal: dict[str, tuple[str, str, int]] = {}
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
                        reason=dec.reason, sub_strategy=pos.sub_strategy))
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
            # risk budget = 0.75% of account equity (x0.5 in the 12% drawdown tier) - set by the Risk Manager
            max_risk = self.gateway.max_trade_risk_usd()
            common = dict(bid=bid, ask=ask, market=market, strategy_capital=max_risk, risk_pct=1.0,
                          available_cash=cap.available, max_order_notional=self.app.risk.max_order_notional_usd,
                          max_position_notional=self.app.risk.max_position_pct_of_strategy * cap.effective_capital)
            trend = self.strategy.evaluate_entry(sig, sell_fee_pct=inst.sell_regulatory_fee_pct, **common)
            brk = evaluate_breakout_entry(df, sig, self.cfg, **common)
            dec, sub = (trend, "trend") if trend.enter else (brk, "breakout")
            info["entry"] = {"trend": trend.reasons, "breakout": brk.reasons}
            if not dec.enter or dec.sizing is None:
                continue
            threshold = self.app.quality.threshold(self.strategy_id)
            q = self._quality(df, sig, dec, bid, ask, book, symbol)
            info["quality"] = {"score": q.score, "grade": q.grade, "threshold": threshold}
            if not q.passes(threshold):
                info["entry"] = f"quality {q.score} < {threshold}"
                self._record(now, symbol, sub, q, threshold, "rejected_quality", f"score {q.score} < {threshold}")
                continue
            intent = OrderIntent(
                strategy_id=self.strategy_id, venue=self.gateway.venue, symbol=symbol, side=Side.BUY,
                order_type=OrderType.LIMIT, quantity=dec.sizing.quantity, reference_price=ask,
                market_data_ts=book.ts, asset_class=AssetClass.STOCK, price=dec.entry_price,
                stop_price=dec.stop, take_profit=dec.take_profit, reason=f"{sub}: " + "; ".join(dec.reasons),
                sub_strategy=sub, quality_score=q.score)
            res = await self.gateway.submit(intent)
            self._record(now, symbol, sub, q, threshold, "submitted" if res.accepted else "rejected_risk",
                         "; ".join(res.reasons), intent.client_order_id)
            info["submitted"] = {"accepted": res.accepted, "why": res.reasons, "strategy": sub}
            if res.accepted:
                open_syms.append(symbol)
        self._persist()


    def _quality(self, df: Any, sig: Any, dec: Any, bid: float, ask: float, book: Any, symbol: str) -> QualityScore:
        """Trade Quality Score from completed daily bars and the live quote (no estimates)."""
        c = self.cfg
        mac = macd(df["close"])
        hist = mac["macd_hist"].dropna()
        vol_avg = df["volume"].iloc[-c.volume_lookback - 1:-1].mean()
        self._closes[symbol] = df["close"]
        held = [s for s in self.cfg.symbols if s != symbol and self.gateway.position(s).is_open]
        matrix = return_correlations({k: v for k, v in self._closes.items() if k in held + [symbol]}, 120)
        corr = [matrix[(symbol, h)] for h in held if (symbol, h) in matrix]
        regime = classify_regime(df, RegimeParams(), c.atr_period)
        notional = dec.sizing.quantity * dec.entry_price
        top = (book.asks[0][0] * book.asks[0][1]) if book.asks else None
        risk = dec.entry_price - dec.stop
        return score_trade(QualityInputs(
            adx=last_valid(adx(df, c.atr_period)), ema_aligned=bool(sig.ema_fast > sig.ema_slow),
            ema_slope_pct=sig.ema_slow_slope, rsi=sig.rsi,
            macd_hist=float(hist.iloc[-1]) if len(hist) else None,
            macd_rising=bool(hist.iloc[-1] > hist.iloc[-2]) if len(hist) > 1 else None,
            volume_ratio=float(df["volume"].iloc[-1] / vol_avg) if vol_avg and vol_avg == vol_avg else None,
            atr_pct=(sig.atr / sig.close) if sig.atr and sig.close else None, atr_band=(0.003, 0.04),
            spread_bps=(ask - bid) / ((ask + bid) / 2) * 10_000 if bid > 0 and ask > 0 else None,
            depth_ratio=(top / notional) if top and notional > 0 else None,
            regime=regime.regime.value if regime.sufficient_data else None,
            reward_risk=(dec.take_profit - dec.entry_price) / risk if risk > 0 else None,
            max_correlation=max(corr) if corr else None, held_positions=len(held)))

    def _record(self, now: datetime, symbol: str, sub: str, q: QualityScore, threshold: int, decision: str,
                why: str = "", cid: str | None = None) -> None:
        key = (now.date().isoformat(), decision, q.score)
        if self._last_signal.get(symbol) == key:
            return  # same evaluation repeated within the day: stored once
        self._last_signal[symbol] = key
        self.repo.add_signal(ts=now, mode=self.mode, strategy_id=self.strategy_id, sub_strategy=sub, symbol=symbol,
                             score=q.score, grade=q.grade, threshold=threshold, decision=decision,
                             reasons=(why + " | " if why else "") + "; ".join(q.reasons), components=q.components,
                             client_order_id=cid)


def _iso(ts: datetime) -> str:
    return ts.isoformat()
