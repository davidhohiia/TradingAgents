"""BOT 01 - Crypto Momentum AI (BTC/USDT, ETH/USDT, SOL/USDT).

Two separate sub-strategies (Momentum, Confirmed Breakout) with separate statistics.
Decisions use COMPLETED bars only. Long only. Spot by default; limited isolated
leverage for BTC/ETH exists only behind the LeverageGuard (disabled by default).
The bot never trades to reach a monthly target.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from trinity.ai.anomaly_detector import TF_SECONDS, AnomalyDetector
from trinity.bots.base import BaseBot
from trinity.bots.crypto_momentum.planner import MomentumPlanner
from trinity.bots.crypto_momentum.strategies.base import TradeState
from trinity.config.settings import AppConfig, CryptoMomentumConfig
from trinity.core.indicators import candles_to_df, floor_to_step
from trinity.core.models import AssetClass, Candle, Fill, OrderIntent, OrderRecord, OrderType, Side
from trinity.core.order_gateway import OrderGateway


def completed(candles: list[Candle], now: datetime, timeframe: str) -> list[Candle]:
    tf = TF_SECONDS.get(timeframe, 3600)
    return [c for c in candles if c.ts + timedelta(seconds=tf) <= now]


class CryptoMomentumBot(BaseBot):
    bot_type = "crypto_momentum"

    def __init__(self, cfg: CryptoMomentumConfig, app_config: AppConfig, anomaly: AnomalyDetector | None = None,
                 leverage_gateway: OrderGateway | None = None, leverage: float = 1.0, **kwargs: Any) -> None:
        super().__init__(strategy_id=cfg.strategy_id, loop_interval=cfg.loop_interval_seconds, **kwargs)
        self.cfg = cfg
        self.app = app_config
        self.planner = MomentumPlanner(cfg, app_config.risk.correlation_threshold,
                                       app_config.risk.correlation_lookback_bars)
        self.anomaly = anomaly or AnomalyDetector()
        self.leverage_gateway = leverage_gateway
        self.leverage = leverage if leverage_gateway is not None else 1.0
        raw = self.load_state()
        self.trades: dict[str, TradeState] = {s: TradeState(**v) for s, v in raw.get("trades", {}).items()}
        self.cooldown: dict[str, str] = raw.get("cooldown", {})
        self.last_entry_bar: dict[str, str] = raw.get("last_entry_bar", {})
        self.pending: dict[str, dict[str, Any]] = raw.get("pending", {})  # entry cid -> signal info
        for gw in (self.gateway, self.leverage_gateway):
            if gw is not None:
                gw.on_fill(self._on_fill)

    def _persist(self) -> None:
        self.save_state({"trades": {s: asdict(t) for s, t in self.trades.items()}, "cooldown": self.cooldown,
                         "last_entry_bar": self.last_entry_bar, "pending": self.pending})

    def _gw_for(self, symbol: str) -> OrderGateway:
        assert self.gateway is not None
        if self.leverage_gateway is not None and symbol in self.app.leverage.symbols and self.leverage > 1:
            return self.leverage_gateway
        return self.gateway

    # ------------------------------------------------------------------ fills
    async def _on_fill(self, order: OrderRecord, fill: Fill) -> None:
        gw = self._gw_for(order.symbol)
        if order.side == Side.BUY:
            info = self.pending.get(order.client_order_id, {})
            pos = gw.position(order.symbol)
            st = self.trades.get(order.symbol)
            if st is None:
                self.trades[order.symbol] = TradeState(
                    sub_strategy=order.sub_strategy, entry=pos.avg_entry_price,
                    initial_stop=order.stop_price or pos.avg_entry_price * 0.95,
                    take_profit=order.take_profit or pos.avg_entry_price * 1.1, highest_close=fill.price,
                    trailing_atr_multiplier=info.get("trail_mult", 2.5),
                    trailing_activation_r=info.get("trail_act", 1.0), level=info.get("level"))
            else:
                st.entry = pos.avg_entry_price
            kind = "position_opened"
        else:
            if not gw.position(order.symbol).is_open:
                self.trades.pop(order.symbol, None)
                self.cooldown[order.symbol] = (self.clock.now() + timedelta(
                    seconds=TF_SECONDS.get(self.cfg.timeframe, 3600) * self.cfg.cooldown_bars_after_exit)).isoformat()
            kind = ("stop_loss" if "stop" in order.reason else
                    "take_profit" if "take profit" in order.reason else "position_closed")
        self._persist()
        await self.notifier.notify(
            kind, f"[{self.mode.value.upper()}] MOMENTUM/{order.sub_strategy or '-'} {order.side.value.upper()} "
                  f"{fill.quantity:.6g} {order.symbol} @ {fill.price:.2f}"
                  + (f" PnL ${fill.realized_pnl:+.2f}" if order.side == Side.SELL else "")
                  + (f" ({order.reason[:120]})" if order.reason else ""))

    # ------------------------------------------------------------------ step
    async def step(self) -> None:
        assert self.gateway is not None and self.market is not None
        await self.gateway.sync()
        if self.leverage_gateway is not None:
            await self.leverage_gateway.sync()
        now = self.clock.now()
        feats, books = {}, {}
        info: dict[str, Any] = {"symbols": {}}
        for symbol in self.cfg.symbols:
            candles = await self.market.fetch_ohlcv(symbol, self.cfg.timeframe, self.cfg.history_bars)
            book = await self.market.fetch_order_book(symbol, 10)
            issues = self.anomaly.check_book(book, now, max_age_s=self.app.risk.price_age_limit(AssetClass.CRYPTO))
            issues += self.anomaly.check_candles(candles, now, self.cfg.timeframe)
            if issues:
                info["symbols"][symbol] = {"skipped": issues}
                continue
            done = completed(candles, now, self.cfg.timeframe)
            feats[symbol] = self.planner.features(candles_to_df(done))
            books[symbol] = book
        matrix = self.planner.correlations(feats)
        if matrix:
            self.gateway.update_correlations(matrix)
        info["correlations"] = {f"{a}~{b}": round(v, 2) for (a, b), v in matrix.items() if a < b}

        # ---- exits first
        for symbol, st in list(self.trades.items()):
            gw = self._gw_for(symbol)
            pos = gw.position(symbol)
            if not pos.is_open:
                if not gw.active_orders(symbol):
                    self.trades.pop(symbol, None)
                continue
            if symbol not in feats or not self.planner.ready(feats[symbol]):
                continue
            book = books[symbol]
            dec = self.planner.exit_decision(feats[symbol], st, book.best_bid or 0.0)
            gw._engine.positions.set_protection(self.strategy_id, gw.venue, symbol, st.active_stop,  # noqa: SLF001
                                               st.take_profit)
            if dec.exit and not any(o.side == Side.SELL for o in gw.active_orders(symbol)):
                market = gw._engine.venues[gw.venue].market_info(symbol)  # noqa: SLF001
                res = await gw.submit(OrderIntent(
                    strategy_id=self.strategy_id, venue=gw.venue, symbol=symbol, side=Side.SELL,
                    order_type=OrderType.MARKET, quantity=floor_to_step(pos.quantity, market.qty_step),
                    reference_price=book.best_bid or 0.0, market_data_ts=book.ts, asset_class=AssetClass.CRYPTO,
                    reduce_only=True, protective_exit=dec.protective, reason=dec.reason, sub_strategy=st.sub_strategy,
                    leverage=pos.leverage))
                info["symbols"].setdefault(symbol, {})["exit"] = {"reason": dec.reason, "accepted": res.accepted,
                                                                   "why": res.reasons}

        # ---- entries
        locks = self.gateway.risk_locked()
        held = [p.symbol for p in self.gateway.open_positions_all()]
        skip = set(held) | {s for s, until in self.cooldown.items() if now < datetime.fromisoformat(until)}
        skip |= {o.symbol for o in self.gateway.active_orders() if o.side == Side.BUY}
        cands, notes = self.planner.candidates(feats, skip)
        for sym, n in notes.per_symbol.items():
            info["symbols"].setdefault(sym, {})["entry"] = n
        if locks or self.paused:
            info["entries"] = f"blocked by risk locks {locks}"
        else:
            selected = {c.symbol for c in self.planner.select(cands, held, matrix, notes)}
            info["correlation_filter"] = notes.correlation
            cap = self.gateway.capital()
            max_risk = self.gateway.max_trade_risk_usd()
            threshold = self.app.quality.threshold(self.strategy_id)
            for sig in cands:
                bar_key = str(feats[sig.symbol].index[-1])
                if self.last_entry_bar.get(sig.symbol) == bar_key:
                    continue  # one entry decision per completed bar
                self.last_entry_bar[sig.symbol] = bar_key
                gw = self._gw_for(sig.symbol)
                book = books[sig.symbol]
                ask = book.best_ask or sig.entry
                spread_bps = (book.spread_pct * 10_000) if book.spread_pct is not None else None
                price = ask * (1 + self.cfg.entry_limit_offset_bps / 10_000)
                stop = min(sig.stop, price - sig.risk_per_unit)  # keep the planned risk distance from fill price
                market = gw._engine.venues[gw.venue].market_info(sig.symbol)  # noqa: SLF001
                lev = self.leverage if gw is self.leverage_gateway else 1.0
                max_notional = min(self.app.risk.max_order_notional_usd,
                                   self.app.risk.max_position_pct_of_strategy * cap.effective_capital * lev)
                qty = self.planner.size(sig, max_risk_usd=max_risk, available=cap.available,
                                        max_notional=max_notional, market=market, price=price, leverage=lev)
                depth = sum(p * q for p, q in book.asks[:5]) if book.asks else None
                q = self.planner.quality(feats[sig.symbol], sig, spread_bps=spread_bps, depth_usd=depth,
                                         order_usd=qty * price, held=held, matrix=matrix)
                sym_info = info["symbols"].setdefault(sig.symbol, {})
                sym_info["quality"] = {"score": q.score, "grade": q.grade, "threshold": threshold}

                def record(decision: str, why: str = "", cid: str | None = None, sig=sig, q=q) -> None:  # noqa: ANN001
                    self.repo.add_signal(ts=now, mode=self.mode, strategy_id=self.strategy_id,
                                         sub_strategy=sig.strategy, symbol=sig.symbol, score=q.score, grade=q.grade,
                                         threshold=threshold, decision=decision,
                                         reasons=(why + " | " if why else "") + "; ".join(q.reasons),
                                         components=q.components, client_order_id=cid)

                if sig.symbol not in selected:
                    record("rejected_correlation")
                    continue
                if spread_bps is None or spread_bps > self.cfg.max_spread_bps:
                    sym_info["entry"] = [f"spread too wide {spread_bps}"]
                    record("skipped_spread", f"spread {spread_bps}bps > {self.cfg.max_spread_bps}")
                    continue
                if qty <= 0:
                    sym_info["entry"] = ["size below venue minimum after risk/capital caps"]
                    record("skipped_size", "minimum order exceeds the allowed risk/capital")
                    continue
                if not q.passes(threshold):
                    sym_info["entry"] = [f"quality {q.score} < {threshold}"]
                    record("rejected_quality", f"score {q.score} < {threshold}")
                    continue
                intent = OrderIntent(
                    strategy_id=self.strategy_id, venue=gw.venue, symbol=sig.symbol, side=Side.BUY,
                    order_type=OrderType.LIMIT, quantity=qty, reference_price=ask, market_data_ts=book.ts,
                    asset_class=AssetClass.CRYPTO, price=price, stop_price=stop,
                    take_profit=price + (sig.take_profit - sig.entry), reason=f"{sig.strategy}: {sig.reasons[0]}"[:300],
                    sub_strategy=sig.strategy, leverage=lev, quality_score=q.score)
                self.pending[intent.client_order_id] = {"trail_mult": sig.trailing_atr_multiplier,
                                                        "trail_act": sig.trailing_activation_r, "level": sig.level}
                res = await gw.submit(intent)
                record("submitted" if res.accepted else "rejected_risk", "; ".join(res.reasons), intent.client_order_id)
                sym_info["submitted"] = {"accepted": res.accepted, "why": res.reasons, "strategy": sig.strategy}
                if not res.accepted:
                    self.pending.pop(intent.client_order_id, None)
                cap = self.gateway.capital()
        # cancel entry orders that did not fill within one bar
        tf = TF_SECONDS.get(self.cfg.timeframe, 3600)
        for o in self.gateway.active_orders():
            if o.side == Side.BUY and now - o.created_at > timedelta(seconds=min(tf, 900)):
                await self.gateway.cancel(o.client_order_id, "entry not filled in time")
        active_ids = {o.client_order_id for o in self.gateway.active_orders()}
        self.pending = {k: v for k, v in self.pending.items() if k in active_ids}
        self._persist()
        self.info = info
