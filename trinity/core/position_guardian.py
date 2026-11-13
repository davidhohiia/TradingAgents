"""Position Guardian: protective stop enforcement independent of the bots (SPEC_4.0 A3).

Runs every ``emergency.guardian_interval_seconds`` in every mode, also while trading is paused,
stopped or in emergency. For every open position it:
* refreshes the liquidation-side price (bid) from the venue;
* if bid <= stop and no exit order is working, submits a PROTECTIVE market exit through the
  Execution Engine (so the Risk Engine still checks it);
* during an emergency: gives positions without a stop an emergency stop and keeps retrying the
  cancellation of unfilled entry orders that could not be canceled earlier;
* tracks venue connectivity and reports loss / restoration of the connection.
It never opens positions and never liquidates a position that has not reached its stop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trinity.config.settings import AppConfig
from trinity.core.clock import Clock
from trinity.core.execution_engine import ExecutionEngine
from trinity.core.indicators import floor_to_step
from trinity.core.log import get_logger
from trinity.core.models import OrderIntent, OrderType, Side
from trinity.exchanges.base_exchange import ExchangeError
from trinity.notifications.notifier import Notifier

log = get_logger(__name__)


@dataclass
class GuardReport:
    checked: int = 0
    exits_submitted: list[str] = field(default_factory=list)
    stops_assigned: list[str] = field(default_factory=list)
    unguarded: list[str] = field(default_factory=list)  # price unavailable / market closed
    entries_canceled: int = 0


class PositionGuardian:
    def __init__(self, engine: ExecutionEngine, config: AppConfig, notifier: Notifier, clock: Clock) -> None:
        self.engine = engine
        self.config = config
        self.notifier = notifier
        self.clock = clock
        self.venue_up: dict[str, bool] = {}
        self._warned_closed: set[str] = set()

    def emergency_active(self) -> bool:
        return self.engine.risk.state.emergency_stop

    async def _price(self, venue_name: str, symbol: str) -> tuple[float | None, Any]:
        venue = self.engine.venues.get(venue_name)
        if venue is None:
            return None, None
        try:
            t = await venue.fetch_ticker(symbol)
        except ExchangeError as exc:
            await self._venue_state(venue_name, False, str(exc))
            return None, None
        await self._venue_state(venue_name, True, "")
        price = t.bid or t.last
        return (price if price else None), t

    async def _venue_state(self, venue: str, up: bool, error: str) -> None:
        before = self.venue_up.get(venue)
        self.venue_up[venue] = up
        if before is not False and not up:
            self.engine.repo.add_risk_event(self.engine.mode, "CRITICAL", "CONNECTION_LOST",
                                            f"{venue}: {error[:200]}", ts=self.clock.now())
            await self.notifier.notify("connection_lost", f"🔌 Connection to {venue} lost ({error[:120]}). Open "
                                       "positions there cannot be closed until it is restored; retrying.", "CRITICAL")
        elif before is False and up:
            self.engine.repo.add_risk_event(self.engine.mode, "INFO", "CONNECTION_RESTORED", venue, ts=self.clock.now())
            await self.notifier.notify("connection_restored", f"✅ Connection to {venue} restored. "
                                       "Position protection active again.", "INFO")

    async def run_once(self) -> GuardReport:
        rep = GuardReport()
        engine = self.engine
        emergency = self.emergency_active()
        if emergency:
            rep.entries_canceled = await engine.cancel_where(side=Side.BUY, reason="emergency protocol (retry)")
        for p in engine.positions.all(open_only=True):
            rep.checked += 1
            key = f"{p.strategy_id}:{p.symbol}"
            price, ticker = await self._price(p.venue, p.symbol)
            if price is None:
                rep.unguarded.append(f"{key}: price unavailable")
                continue
            engine.positions.mark_to_market(p.symbol, price, self.clock.now())
            if p.stop_price is None:
                if emergency:
                    stop = price * (1 - self.config.emergency.default_stop_pct)
                    engine.positions.set_protection(p.strategy_id, p.venue, p.symbol, stop, p.take_profit)
                    rep.stops_assigned.append(f"{key}@{stop:.2f}")
                else:
                    log.warning("position %s has no stop (bot should set one)", key)
                continue
            if price > p.stop_price:
                continue
            stop = p.stop_price
            sells = [o for o in engine.repo.list_active_orders(engine.mode, strategy_id=p.strategy_id, symbol=p.symbol)
                     if o.side == Side.SELL]
            if any(o.order_type == OrderType.MARKET for o in sells):
                continue  # a protective market exit is already working
            for o in sells:  # resting take-profit limits above the market would never fill in a crash
                await engine.cancel(o.client_order_id, f"superseded by protective stop exit (stop {stop:.2f})")
            if any(o.side == Side.SELL for o in engine.repo.list_active_orders(engine.mode, strategy_id=p.strategy_id,
                                                                             symbol=p.symbol)):
                rep.unguarded.append(f"{key}: could not cancel resting exit order, retrying")
                continue
            venue = engine.venues[p.venue]
            try:
                market_open = await venue.is_market_open(self.clock.now())
            except ExchangeError:
                market_open = False
            if not market_open:
                rep.unguarded.append(f"{key}: market closed, stop {p.stop_price:.2f} breached")
                if key not in self._warned_closed:
                    self._warned_closed.add(key)
                    await self.notifier.notify("stop_loss", f"⚠️ {key} below stop {p.stop_price:.2f} but the market "
                                               "is closed; protective exit at the next session.", "WARNING")
                continue
            self._warned_closed.discard(key)
            m = venue.market_info(p.symbol)
            qty = floor_to_step(p.quantity, m.qty_step)
            if qty <= 0:
                continue
            res = await engine.submit(OrderIntent(
                strategy_id=p.strategy_id, venue=p.venue, symbol=p.symbol, side=Side.SELL,
                order_type=OrderType.MARKET, quantity=qty, reference_price=price, market_data_ts=ticker.ts,
                asset_class=p.asset_class, reduce_only=True, protective_exit=True, sub_strategy=p.sub_strategy,
                reason=f"guardian stop {stop:.2f} hit (bid {price:.2f})", leverage=p.leverage))
            if res.accepted:
                rep.exits_submitted.append(key)
                await self.notifier.notify("stop_loss", f"🛡 Guardian protective exit {key}: bid {price:.2f} <= stop "
                                           f"{stop:.2f}", "WARNING")
            else:
                log.warning("guardian exit for %s not accepted: %s", key, res.reasons)
        return rep
