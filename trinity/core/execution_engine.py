"""Central execution engine (the ONLY path to a venue's trading API).

Flow for every order intent:
    1. duplicate / conflict checks (client order id, same-price duplicates, self-trade)
    2. quantity/price rounding to venue precision
    3. Global Risk Manager check (fresh portfolio snapshot)
    4. capital reservation (idempotent per client order id)
    5. write-ahead persistence (status PENDING_SUBMIT) so a crash can be reconciled
    6. submission to the venue (never blindly retried: on timeout, lookup by client id)
    7. fill processing: executions + positions + order state in ONE DB transaction,
       idempotent per (client order id, cumulative filled quantity)
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from trinity.config.settings import AppConfig
from trinity.core.capital_allocator import CapitalAllocator
from trinity.core.clock import Clock, RealClock
from trinity.core.indicators import floor_to_step, round_to_step
from trinity.core.log import get_logger, log_event
from trinity.core.models import (
    ExchangeOrder,
    Fill,
    MarketInfo,
    Mode,
    OrderIntent,
    OrderRecord,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    SubmitResult,
)
from trinity.core.position_manager import PositionManager
from trinity.database.repository import Repository
from trinity.exchanges.base_exchange import (
    AuthError,
    BaseExchange,
    ExchangeError,
    ExchangeUnavailable,
    NotConfigured,
    OrderNotFound,
    OrderRejected,
)
from trinity.risk.circuit_breaker import CircuitBreakerRegistry
from trinity.risk.exposure_manager import ExposureManager
from trinity.risk.risk_manager import RiskContext, RiskManager

log = get_logger(__name__)

FillCallback = Callable[[OrderRecord, Fill], Awaitable[None] | None]
EventCallback = Callable[[str, dict[str, Any]], Awaitable[None] | None]

PENDING_SUBMIT_TIMEOUT = timedelta(minutes=2)
EPS = 1e-10


@dataclass
class ReconcileReport:
    venue: str
    ok: bool = True
    issues: list[str] = field(default_factory=list)
    updated_orders: int = 0
    notes: list[str] = field(default_factory=list)

    def issue(self, msg: str) -> None:
        self.ok = False
        self.issues.append(msg)


class ExecutionEngine:
    def __init__(
        self,
        *,
        config: AppConfig,
        repo: Repository,
        mode: Mode,
        risk: RiskManager,
        allocator: CapitalAllocator,
        positions: PositionManager,
        venues: dict[str, BaseExchange],
        breakers: CircuitBreakerRegistry,
        clock: Clock | None = None,
        usdt_usd_rate: float = 1.0,
        contributed_fn: Callable[[], float] | None = None,
    ) -> None:
        self.config = config
        self.repo = repo
        self.mode = mode
        self.risk = risk
        self.allocator = allocator
        self.positions = positions
        self.venues = venues
        self.breakers = breakers
        self.clock = clock or RealClock()
        self.usdt_usd_rate = usdt_usd_rate
        self.contributed_fn = contributed_fn or (lambda: config.capital.total_capital_usd)
        self.exposure = ExposureManager(config.correlation_group)
        self.venue_connected: dict[str, bool] = dict.fromkeys(venues, True)
        self._lock = asyncio.Lock()
        self._fill_callbacks: dict[str, list[FillCallback]] = {}
        self._event_callbacks: list[EventCallback] = []
        if mode == Mode.LIVE and any(v.is_paper for v in venues.values()):
            raise ValueError("live execution engine cannot use paper venues")
        if mode == Mode.PAPER and any(not v.is_paper for v in venues.values()):
            raise ValueError("paper execution engine must only use paper venues")

    # ------------------------------------------------------------------ account metrics
    def account_equity(self) -> float:
        """Contributed capital (deposits) + net realized + unrealized PnL. Deposits are not profit."""
        t = self.positions.totals()
        return self.contributed_fn() + t["realized"] + t["unrealized"]

    def open_risk(self, active: list[OrderRecord] | None = None) -> float:
        """Risk-to-stop of all open positions plus pending entry orders."""
        active = active if active is not None else self.repo.list_active_orders(self.mode)
        risk = sum(p.risk_to_stop for p in self.positions.all(open_only=True))
        for o in active:
            if o.side != Side.BUY:
                continue
            px = o.price or o.avg_fill_price or 0.0
            risk += o.remaining * (px - o.stop_price if o.stop_price and o.stop_price < px else px)
        return risk

    # ------------------------------------------------------------------ callbacks
    def on_fill(self, strategy_id: str, cb: FillCallback) -> None:
        self._fill_callbacks.setdefault(strategy_id, []).append(cb)

    def on_event(self, cb: EventCallback) -> None:
        self._event_callbacks.append(cb)

    async def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        for cb in self._event_callbacks:
            try:
                res = cb(kind, payload)
                if asyncio.iscoroutine(res):
                    await res
            except Exception:  # noqa: BLE001
                log.exception("event callback failed")

    # ------------------------------------------------------------------ helpers
    def _venue(self, name: str) -> BaseExchange:
        if name not in self.venues:
            raise OrderRejected(f"unknown venue {name}")
        return self.venues[name]

    def _fee_rates(self) -> dict[str, float]:
        out = {}
        for v in self.venues.values():
            for sym in self.config.instruments:
                try:
                    m = v.market_info(sym)
                    out[sym] = max(m.maker_fee, m.taker_fee)
                except ExchangeError:
                    continue
        return out

    def _to_usd(self, amount: float, currency: str | None, price: float, market: MarketInfo) -> float:
        if not amount:
            return 0.0
        if currency is None or currency == market.quote:
            rate = self.usdt_usd_rate if market.quote in ("USDT", "USDC") else 1.0
            return amount * rate
        if currency == market.base:
            return amount * price * (self.usdt_usd_rate if market.quote in ("USDT", "USDC") else 1.0)
        if currency in ("USD",):
            return amount
        log.warning("fee in unsupported currency %s: estimated from fee rate", currency)
        return -1.0  # signal: unknown

    async def _record_failure(self, venue: str, exc: Exception) -> None:
        opened = self.breakers.get(venue).record_failure(f"{type(exc).__name__}: {exc}")
        if opened:
            self.venue_connected[venue] = False
            self.repo.add_risk_event(self.mode, "CRITICAL", "API_CIRCUIT_OPEN",
                                     f"circuit breaker opened for {venue}: {exc}", ts=self.clock.now())
            await self._emit("api_error", {"venue": venue, "error": str(exc)})

    def _record_success(self, venue: str) -> None:
        self.breakers.get(venue).record_success()
        self.venue_connected[venue] = True

    # ------------------------------------------------------------------ submission
    async def submit(self, intent: OrderIntent) -> SubmitResult:
        async with self._lock:
            return await self._submit_locked(intent)

    async def _submit_locked(self, intent: OrderIntent) -> SubmitResult:
        cid = intent.client_order_id
        now = self.clock.now()

        def reject(*reasons: str, duplicate: bool = False) -> SubmitResult:
            self.repo.add_risk_event(
                self.mode, "INFO", "ORDER_REJECTED", "; ".join(reasons), strategy_id=intent.strategy_id,
                symbol=intent.symbol, details={"side": intent.side.value, "qty": intent.quantity,
                                               "price": intent.price, "reason": intent.reason}, ts=now,
            )
            return SubmitResult(False, cid, None, list(reasons), duplicate=duplicate)

        if self.repo.order_exists(cid):
            return reject(f"duplicate client order id {cid}", duplicate=True)
        try:
            venue = self._venue(intent.venue)
            market = venue.market_info(intent.symbol)
        except ExchangeError as exc:
            return reject(str(exc))
        if intent.asset_class != market.asset_class:
            return reject("asset class mismatch")

        # precision
        intent.quantity = floor_to_step(intent.quantity, market.qty_step)
        if intent.price is not None:
            intent.price = round_to_step(intent.price, market.price_step)
        if intent.quantity <= 0:
            return reject("quantity rounds to zero at venue precision")

        # duplicate / conflicting orders
        active = self.repo.list_active_orders(self.mode)
        for o in active:
            if o.venue != intent.venue or o.symbol != intent.symbol:
                continue
            if o.strategy_id == intent.strategy_id and o.side == intent.side:
                same_price = (o.price is None and intent.price is None) or (
                    o.price is not None and intent.price is not None
                    and abs(o.price - intent.price) < market.price_step * 0.5 + EPS)
                if same_price:
                    return reject(f"duplicate active {o.side} order at same price ({o.client_order_id})",
                                  duplicate=True)
            if o.strategy_id != intent.strategy_id and o.side != intent.side:
                buy_px = intent.effective_price if intent.side == Side.BUY else (o.price or float("inf"))
                sell_px = intent.effective_price if intent.side == Side.SELL else (o.price or 0.0)
                if buy_px >= sell_px:
                    return reject(f"conflicting order from {o.strategy_id} would self-trade")

        positions_all = self.positions.all()
        open_positions = [p for p in positions_all if p.is_open]
        exposure = self.exposure.snapshot(open_positions, active)
        realized = self.positions.realized_by_strategy().get(intent.strategy_id, 0.0)
        cap = self.allocator.compute(intent.strategy_id, positions_all, active, realized, self._fee_rates())
        funding = None
        if intent.leverage > 1.0 and hasattr(venue, "fetch_funding_rate"):
            try:
                funding = await venue.fetch_funding_rate(intent.symbol)
            except ExchangeError as exc:
                log.warning("funding rate unavailable for %s: %s", intent.symbol, exc)
        ctx = RiskContext(
            exposure=exposure, market=market, strategy_capital=cap.effective_capital,
            strategy_available=cap.available, now=now,
            venue_connected=self.venue_connected.get(intent.venue, False),
            equity=self.account_equity(), open_risk=self.open_risk(active),
            held_symbols=sorted({p.symbol for p in open_positions}), funding_rate_8h=funding,
        )
        decision = self.risk.check_order(intent, ctx)
        if not decision.approved:
            return reject(*decision.reasons)

        fee_rate = market.taker_fee if intent.order_type == OrderType.MARKET else max(market.maker_fee, market.taker_fee)
        reserve = (intent.notional / max(intent.leverage, 1.0) + intent.notional * fee_rate
                   if intent.side == Side.BUY else 0.0)
        if reserve and not self.allocator.reserve_inflight(intent.strategy_id, cid, reserve, cap.available):
            return reject("capital reservation failed (insufficient or duplicate)")

        record = OrderRecord(
            client_order_id=cid, strategy_id=intent.strategy_id, venue=intent.venue, symbol=intent.symbol,
            mode=self.mode, side=intent.side, order_type=intent.order_type, quantity=intent.quantity,
            status=OrderStatus.PENDING_SUBMIT, price=intent.price, stop_price=intent.stop_price,
            take_profit=intent.take_profit, reduce_only=intent.side == Side.SELL, reserved_usd=reserve,
            reason=intent.reason, parent_client_order_id=intent.parent_client_order_id,
            created_at=now, updated_at=now, sub_strategy=intent.sub_strategy, leverage=intent.leverage,
        )
        if intent.leverage > 1.0:
            # isolated margin + leverage must be set on the venue BEFORE the order (never increased later)
            prepare = getattr(venue, "prepare_leverage", None)
            if prepare is None:
                self.allocator.release_inflight(cid)
                return reject(f"venue {intent.venue} does not support isolated-margin leverage")
            try:
                await prepare(intent.symbol, intent.leverage)
            except ExchangeError as exc:
                self.allocator.release_inflight(cid)
                return reject(f"could not set isolated leverage: {exc}")
        try:
            self.repo.insert_order(record)  # write-ahead
        finally:
            self.allocator.release_inflight(cid)
        self.risk.record_submission(now)
        log_event(log, 20, "submitting order", cid=cid, strategy=intent.strategy_id, symbol=intent.symbol,
                  side=intent.side.value, type=intent.order_type.value, qty=intent.quantity, price=intent.price,
                  reason=intent.reason, mode=self.mode.value)

        req = OrderRequest(cid, intent.symbol, intent.side, intent.order_type, intent.quantity, intent.price)
        try:
            ex_order = await venue.create_order(req)
            self._record_success(intent.venue)
        except (OrderRejected, NotConfigured, AuthError) as exc:
            record.status = OrderStatus.REJECTED
            record.error = str(exc)[:500]
            record.reserved_usd = 0.0
            self.repo.update_order(record)
            if isinstance(exc, AuthError | NotConfigured):
                await self._record_failure(intent.venue, exc)
            self.repo.add_risk_event(self.mode, "WARNING", "VENUE_REJECTED", str(exc)[:300],
                                     strategy_id=intent.strategy_id, symbol=intent.symbol, ts=now)
            return SubmitResult(False, cid, record, [f"venue rejected: {exc}"])
        except ExchangeUnavailable as exc:
            await self._record_failure(intent.venue, exc)
            # Unknown outcome. Never resend blindly: look the order up by client id.
            found = await self._lookup(venue, record)
            if found is None:
                record.error = f"submit timeout, outcome unknown: {exc}"[:500]
                self.repo.update_order(record)  # stays PENDING_SUBMIT; sync/reconcile resolves it
                return SubmitResult(True, cid, record, ["submitted with unknown outcome; will reconcile"])
            ex_order = found
        await self._apply_update(record, ex_order)
        return SubmitResult(True, cid, record)

    async def _lookup(self, venue: BaseExchange, record: OrderRecord) -> ExchangeOrder | None:
        try:
            found = await venue.fetch_order(record.symbol, record.exchange_order_id, record.client_order_id)
            self._record_success(record.venue)
            return found
        except ExchangeError as exc:
            await self._record_failure(record.venue, exc)
            return None

    # ------------------------------------------------------------------ fills / state
    async def _apply_update(self, record: OrderRecord, ex: ExchangeOrder) -> None:
        venue = self.venues[record.venue]
        market = venue.market_info(record.symbol)
        fills: list[Fill] = []
        realized_total = 0.0
        with self.repo.transaction():
            if ex.exchange_order_id:
                record.exchange_order_id = ex.exchange_order_id
            new_filled = min(ex.filled, record.quantity + EPS) if ex.filled else 0.0
            delta = new_filled - record.filled_quantity
            if delta > EPS:
                old_cost = (record.avg_fill_price or 0.0) * record.filled_quantity
                if ex.average_price:
                    price = (ex.average_price * new_filled - old_cost) / delta
                    if price <= 0 or abs(price / ex.average_price - 1) > 0.2:
                        price = ex.average_price
                else:
                    price = record.price or ex.price or 0.0
                total_fee_usd = self._to_usd(ex.fee, ex.fee_currency, price, market)
                if total_fee_usd < 0 or (ex.fee == 0 and not venue.is_paper):
                    # Fee not reported: conservative estimate with taker rate.
                    total_fee_usd = new_filled * (ex.average_price or price) * market.taker_fee
                fee_delta = max(total_fee_usd - record.fee_usd, 0.0)
                qty_credit = delta
                if record.side == Side.BUY and ex.fee_currency == market.base and ex.fee:
                    # Fee taken from the received asset (e.g. Binance without BNB): credit net quantity.
                    prev_fee_base = record.fee_usd / price if price else 0.0
                    qty_credit = max(delta - max(ex.fee - prev_fee_base, 0.0), 0.0)
                fill = Fill(
                    exec_id=f"{record.client_order_id}:{new_filled:.10f}", client_order_id=record.client_order_id,
                    strategy_id=record.strategy_id, venue=record.venue, symbol=record.symbol, mode=self.mode,
                    side=record.side, quantity=qty_credit, price=price, fee_usd=fee_delta, ts=self.clock.now(),
                    fee=ex.fee, fee_currency=ex.fee_currency, exchange_order_id=record.exchange_order_id,
                    is_maker=ex.is_maker,
                )
                if not self.repo.execution_exists(fill.exec_id) and qty_credit > 0:
                    if record.side == Side.SELL:
                        pos = self.positions.get(record.strategy_id, record.venue, record.symbol)
                        fill.quantity = min(fill.quantity, pos.quantity)
                    fill.realized_pnl = self.positions.apply_fill(
                        fill, exit_reason=record.reason, sub_strategy=record.sub_strategy, leverage=record.leverage,
                        stop_price=record.stop_price, take_profit=record.take_profit,
                        unsellable_qty=max(market.qty_step, market.min_qty))
                    self.repo.insert_execution(fill)
                    realized_total += fill.realized_pnl
                    fills.append(fill)
                record.avg_fill_price = (old_cost + price * delta) / new_filled
                record.filled_quantity = new_filled
                record.fee_usd = record.fee_usd + fee_delta
            status = ex.status
            if status in (OrderStatus.OPEN, OrderStatus.PENDING_SUBMIT) and record.filled_quantity > EPS:
                status = OrderStatus.PARTIALLY_FILLED
            record.status = status
            if status.is_final:
                record.reserved_usd = 0.0
            elif record.side == Side.BUY:
                record.reserved_usd = record.remaining * (record.price or ex.price or 0.0) * (
                    1 + max(market.maker_fee, market.taker_fee))
            self.repo.update_order(record)
        for f in fills:
            await self._dispatch_fill(record, f)

    async def _dispatch_fill(self, record: OrderRecord, fill: Fill) -> None:
        log_event(log, 20, "order fill", cid=record.client_order_id, strategy=record.strategy_id,
                  symbol=record.symbol, side=record.side.value, qty=fill.quantity, price=fill.price,
                  fee_usd=fill.fee_usd, realized=fill.realized_pnl, mode=self.mode.value)
        await self._emit("fill", {"order": record, "fill": fill})
        for cb in self._fill_callbacks.get(record.strategy_id, []):
            try:
                res = cb(record, fill)
                if asyncio.iscoroutine(res):
                    await res
            except Exception:  # noqa: BLE001
                log.exception("fill callback failed for %s", record.strategy_id)

    # ------------------------------------------------------------------ sync / cancel
    async def sync_orders(self, strategy_id: str | None = None) -> int:
        """Poll venues for every active order; process fills and status changes."""
        updated = 0
        async with self._lock:
            for record in self.repo.list_active_orders(self.mode, strategy_id=strategy_id):
                venue = self.venues.get(record.venue)
                if venue is None:
                    continue
                if not self.breakers.allow(record.venue):
                    continue
                try:
                    ex = await venue.fetch_order(record.symbol, record.exchange_order_id, record.client_order_id)
                    self._record_success(record.venue)
                except ExchangeError as exc:
                    await self._record_failure(record.venue, exc)
                    continue
                if ex is None:
                    age = self.clock.now() - record.created_at
                    if record.status == OrderStatus.PENDING_SUBMIT and age < PENDING_SUBMIT_TIMEOUT:
                        continue
                    record.status = OrderStatus.FAILED
                    record.error = "order not found on venue"
                    record.reserved_usd = 0.0
                    self.repo.update_order(record)
                    self.repo.add_risk_event(self.mode, "WARNING", "ORDER_NOT_FOUND",
                                             f"{record.client_order_id} not found on {record.venue}",
                                             strategy_id=record.strategy_id, symbol=record.symbol,
                                             ts=self.clock.now())
                    updated += 1
                    continue
                before = (record.status, record.filled_quantity)
                await self._apply_update(record, ex)
                if (record.status, record.filled_quantity) != before:
                    updated += 1
        return updated

    async def cancel(self, client_order_id: str, reason: str = "") -> bool:
        async with self._lock:
            return await self._cancel_locked(client_order_id, reason)

    async def _cancel_locked(self, client_order_id: str, reason: str) -> bool:
        record = self.repo.get_order(client_order_id)
        if record is None or not record.status.is_active:
            return False
        venue = self.venues.get(record.venue)
        if venue is None:
            return False
        try:
            ex = await venue.cancel_order(record.symbol, record.exchange_order_id, record.client_order_id)
            self._record_success(record.venue)
        except OrderNotFound:
            ex = await self._lookup(venue, record)
            if ex is None:
                record.status = OrderStatus.FAILED
                record.error = "cancel: order not found on venue"
                record.reserved_usd = 0.0
                self.repo.update_order(record)
                return True
        except ExchangeError as exc:
            await self._record_failure(record.venue, exc)
            log.warning("cancel failed for %s: %s", client_order_id, exc)
            return False
        await self._apply_update(record, ex)
        log_event(log, 20, "order canceled", cid=client_order_id, reason=reason, status=record.status.value)
        return True

    async def cancel_where(
        self, *, strategy_id: str | None = None, side: Side | None = None, symbol: str | None = None,
        reason: str = "",
    ) -> int:
        n = 0
        async with self._lock:
            for o in self.repo.list_active_orders(self.mode, strategy_id=strategy_id, symbol=symbol):
                if side is not None and o.side != side:
                    continue
                if await self._cancel_locked(o.client_order_id, reason):
                    n += 1
        return n

    # ------------------------------------------------------------------ reconciliation
    async def reconcile(self, venue_name: str) -> ReconcileReport:
        """Compare venue state with the local DB. Any mismatch -> report.ok False."""
        rep = ReconcileReport(venue_name)
        venue = self.venues[venue_name]
        async with self._lock:
            try:
                open_on_venue = await venue.fetch_open_orders()
                self._record_success(venue_name)
            except ExchangeError as exc:
                await self._record_failure(venue_name, exc)
                rep.issue(f"cannot fetch open orders: {exc}")
                return rep
            db_active = {o.client_order_id: o for o in self.repo.list_active_orders(self.mode, venue=venue_name)}
            for ex in open_on_venue:
                if not ex.client_order_id or ex.client_order_id not in db_active:
                    if ex.client_order_id and self.repo.order_exists(ex.client_order_id):
                        rec = self.repo.get_order(ex.client_order_id)
                        assert rec is not None
                        await self._apply_update(rec, ex)
                        rep.notes.append(f"order {ex.client_order_id} re-opened from venue state")
                        rep.updated_orders += 1
                    else:
                        rep.issue(f"unknown open order on venue: {ex.exchange_order_id} {ex.symbol} {ex.side}")
            for cid, rec in db_active.items():
                try:
                    ex = await venue.fetch_order(rec.symbol, rec.exchange_order_id, cid)
                except ExchangeError as exc:
                    rep.issue(f"cannot fetch order {cid}: {exc}")
                    continue
                if ex is None:
                    rec.status = OrderStatus.FAILED
                    rec.error = "not found on venue during reconciliation"
                    rec.reserved_usd = 0.0
                    self.repo.update_order(rec)
                    rep.notes.append(f"{cid} not on venue -> FAILED")
                else:
                    await self._apply_update(rec, ex)
                rep.updated_orders += 1
            # balances vs positions (only meaningful for real venues; paper is self-consistent)
            try:
                balances = await venue.fetch_balances()
            except NotConfigured as exc:
                rep.issue(str(exc))
                return rep
            except ExchangeError as exc:
                rep.issue(f"cannot fetch balances: {exc}")
                return rep
            for b in balances.values():
                self.repo.upsert_balance(venue_name, self.mode, b.asset, b.free, b.used, None)
            held: dict[str, float] = {}
            for p in self.positions.all(open_only=True):
                if p.venue == venue_name:
                    base = venue.market_info(p.symbol).base
                    held[base] = held.get(base, 0.0) + p.quantity
            if callable(getattr(venue, "fetch_positions", None)):
                try:
                    broker_pos = await venue.fetch_positions()
                except ExchangeError as exc:
                    rep.issue(f"cannot fetch broker positions: {exc}")
                    broker_pos = {}
                for sym, qty in held.items():
                    have = broker_pos.get(sym, (0.0, 0.0))[0]
                    if have + 1e-6 < qty:
                        rep.issue(f"broker position {sym}={have} < local {qty}")
            else:
                for asset, qty in held.items():
                    have = balances.get(asset)
                    total = have.total if have else 0.0
                    if total + 1e-8 < qty * 0.995:  # tolerance for fee-in-base rounding
                        rep.issue(f"venue balance {asset}={total} < local positions {qty}")
        self.repo.add_system_event("INFO" if rep.ok else "CRITICAL", "execution", "reconcile",
                                   f"{venue_name}: {'OK' if rep.ok else 'MISMATCH'}",
                                   {"issues": rep.issues, "notes": rep.notes}, ts=self.clock.now())
        return rep
