"""Crypto Grid AI bot: adaptive grid on liquid spot pairs (BTC/USDT, ETH/USDT)."""

from __future__ import annotations

from typing import Any

from trinity.ai.anomaly_detector import AnomalyDetector
from trinity.bots.base import BaseBot
from trinity.bots.crypto_grid.strategy import GridStrategy, GridSymbolState, Lot
from trinity.config.settings import GridStrategyConfig
from trinity.core.indicators import candles_to_df
from trinity.core.models import (
    AssetClass,
    Fill,
    OrderIntent,
    OrderRecord,
    Side,
)

EPS = 1e-10


class CryptoGridBot(BaseBot):
    bot_type = "crypto_grid"

    def __init__(self, cfg: GridStrategyConfig, anomaly: AnomalyDetector | None = None, **kwargs: Any) -> None:
        super().__init__(strategy_id=cfg.strategy_id, loop_interval=cfg.loop_interval_seconds, **kwargs)
        self.cfg = cfg
        self.strategy = GridStrategy(cfg)
        self.anomaly = anomaly or AnomalyDetector()
        self.states: dict[str, GridSymbolState] = {}
        self._restore()
        if self.gateway is not None:
            self.gateway.on_fill(self._on_fill)

    # ------------------------------------------------------------------ persistence
    def _restore(self) -> None:
        raw = self.load_state()
        self.states = {s: GridSymbolState.from_dict(raw.get(s)) for s in self.cfg.symbols}

    def _persist(self) -> None:
        self.save_state({s: st.to_dict() for s, st in self.states.items()})

    # ------------------------------------------------------------------ fills
    async def _on_fill(self, order: OrderRecord, fill: Fill) -> None:
        st = self.states.get(order.symbol)
        if st is None:
            return
        if order.side == Side.BUY:
            lot = next((lot_ for lot_ in st.lots if lot_.buy_cid == order.client_order_id), None)
            if lot is None:
                lot = Lot(order.client_order_id, 0.0, order.price or fill.price, target=order.take_profit)
                st.lots.append(lot)
            lot.qty += fill.quantity
            if st.stop_price:
                assert self.gateway is not None
                self.gateway._engine.positions.set_protection(  # noqa: SLF001
                    self.strategy_id, self.gateway.venue, order.symbol, st.stop_price, None)
        else:
            remaining = fill.quantity
            if "grid stop" in order.reason:
                st.lots.clear()
            else:
                for lot in [lot_ for lot_ in st.lots if lot_.sell_cid == order.client_order_id]:
                    take = min(lot.qty, remaining)
                    lot.qty -= take
                    remaining -= take
                st.lots = [lot_ for lot_ in st.lots if lot_.qty > EPS]
        self._persist()
        await self.notifier.notify(
            "position_opened" if order.side == Side.BUY else "position_closed",
            f"[{self.mode.value.upper()}] GRID {order.side.value.upper()} {fill.quantity:.6f} {order.symbol} "
            f"@ {fill.price:.2f} fee ${fill.fee_usd:.4f}"
            + (f" PnL ${fill.realized_pnl:+.4f}" if order.side == Side.SELL else ""),
        )

    def _reconcile_lots(self, symbol: str, st: GridSymbolState) -> None:
        """Keep lot bookkeeping consistent with the authoritative position."""
        assert self.gateway is not None
        pos = self.gateway.position(symbol)
        lot_qty = st.lot_qty
        if lot_qty > pos.quantity + EPS:
            excess = lot_qty - pos.quantity
            for lot in reversed(st.lots):
                take = min(lot.qty, excess)
                lot.qty -= take
                excess -= take
                if excess <= EPS:
                    break
            st.lots = [lot_ for lot_ in st.lots if lot_.qty > EPS]
        elif pos.quantity > lot_qty + EPS:
            st.lots.append(Lot("recovered", pos.quantity - lot_qty, pos.avg_entry_price))

    # ------------------------------------------------------------------ main step
    async def step(self) -> None:
        assert self.gateway is not None and self.market is not None
        await self.gateway.sync()
        locks = self.gateway.risk_locked()
        hard_locked = any(k in locks for k in ("emergency_stop", "drawdown_lock", "live_disabled"))
        allow_new_buys = not locks and not self.paused
        cap = self.gateway.capital()
        max_risk = cap.effective_capital * self.gateway.max_risk_per_trade_pct
        summary: dict[str, Any] = {}
        for symbol in self.cfg.symbols:
            st = self.states[symbol]
            candles = await self.market.fetch_ohlcv(symbol, self.cfg.timeframe, self.cfg.history_bars)
            book = await self.market.fetch_order_book(symbol, 20)
            now = self.clock.now()
            issues = self.anomaly.check_book(book, now, max_age_s=30) + self.anomaly.check_candles(candles, now, self.cfg.timeframe)
            if issues:
                summary[symbol] = {"skipped": issues}
                self.log.warning("%s: market data anomalies, skipping: %s", symbol, issues)
                continue
            df = candles_to_df(candles)
            market = self.gateway._engine.venues[self.gateway.venue].market_info(symbol)  # noqa: SLF001
            self._reconcile_lots(symbol, st)
            pos = self.gateway.position(symbol)
            active = self.gateway.active_orders(symbol)
            plan = self.strategy.plan(
                df=df, book=book, now=now, st=st, position_qty=pos.quantity, active=active, market=market,
                max_risk_usd=max_risk, allow_new_buys=allow_new_buys,
            )
            for cid in dict.fromkeys(plan.cancel):
                await self.gateway.cancel(cid, reason="; ".join(plan.reasons) or "grid maintenance")
            if plan.stop_triggered:
                await self.notifier.notify("stop_loss", f"[{self.mode.value.upper()}] GRID STOP {symbol}: {plan.reasons}",
                                           "WARNING")
            mid = book.mid or 0.0
            for spec in plan.orders:
                if hard_locked and not spec.protective:
                    continue
                if spec.side == Side.BUY and not allow_new_buys:
                    continue
                intent = OrderIntent(
                    strategy_id=self.strategy_id, venue=self.gateway.venue, symbol=symbol, side=spec.side,
                    order_type=spec.order_type, quantity=spec.qty, reference_price=mid, market_data_ts=book.ts,
                    asset_class=AssetClass.CRYPTO, price=spec.price, stop_price=spec.stop_price,
                    take_profit=spec.take_profit, reduce_only=spec.side == Side.SELL,
                    protective_exit=spec.protective, reason=spec.reason,
                )
                # bind lots to the sell BEFORE submitting: an immediate fill must find its lots
                bound = [lot_ for lot_ in st.lots if lot_.buy_cid in spec.lot_buy_cids] if spec.side == Side.SELL else []
                previous = [lot_.sell_cid for lot_ in bound]
                for lot in bound:
                    lot.sell_cid = intent.client_order_id
                res = await self.gateway.submit(intent)
                if not res.accepted:
                    for lot, prev in zip(bound, previous, strict=True):
                        if lot.sell_cid == intent.client_order_id:
                            lot.sell_cid = prev
                    self.log.info("%s order not accepted: %s", symbol, res.reasons)
                    continue
                if spec.side == Side.BUY and spec.level is not None:
                    st.buy_orders[res.client_order_id] = spec.level
                    st.buy_created[res.client_order_id] = now.isoformat()
            summary[symbol] = {
                "regime": plan.regime.regime.value, "regime_reasons": plan.regime.reasons,
                "spacing": st.spacing, "center": st.center, "stop": st.stop_price, "lots": len(st.lots),
                "inventory_qty": pos.quantity, "reasons": plan.reasons, "rejected_levels": plan.rejected_levels[:5],
            }
        self._persist()
        self.info = {"symbols": summary}
