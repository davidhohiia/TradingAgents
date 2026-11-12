"""Adaptive grid strategy (decision logic only; no I/O).

Safety properties (enforced here AND again by the global risk manager):
* fixed notional per level - no martingale, no size increase after losses;
* inventory cap per symbol - no endless averaging down in a falling market;
* regime filter - in bear / strong bear / high-volatility regimes all unfilled buys are
  canceled and no new buys are placed; existing inventory keeps its take-profit sells
  and the grid stop;
* grid stop below the lowest level - protective market exit, followed by a cooldown;
* every level must clear the minimum expected net profit after fees and spread.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

import pandas as pd

from trinity.bots.crypto_grid.signals import (
    adaptive_spacing,
    buy_levels,
    expected_net_profit,
    grid_stop,
    sell_target,
)
from trinity.config.settings import GridStrategyConfig
from trinity.core.indicators import floor_to_step
from trinity.core.market_regime import RegimeResult, classify_regime
from trinity.core.models import MarketInfo, MarketRegime, OrderBook, OrderRecord, OrderType, Side

EPS = 1e-12


@dataclass
class Lot:
    buy_cid: str
    qty: float
    price: float
    sell_cid: str | None = None
    target: float | None = None


@dataclass
class GridSymbolState:
    center: float | None = None
    spacing: float | None = None
    atr: float | None = None
    regime: str | None = None
    built_at: str | None = None
    stop_price: float | None = None
    cooldown_until: str | None = None
    buy_orders: dict[str, float] = field(default_factory=dict)  # cid -> level price
    buy_created: dict[str, str] = field(default_factory=dict)  # cid -> iso time
    lots: list[Lot] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict | None) -> GridSymbolState:
        if not d:
            return cls()
        d = dict(d)
        d["lots"] = [Lot(**x) for x in d.get("lots", [])]
        return cls(**d)

    @property
    def lot_qty(self) -> float:
        return sum(lot_.qty for lot_ in self.lots)

    def reset_grid(self) -> None:
        self.center = None
        self.spacing = None
        self.atr = None
        self.built_at = None


@dataclass
class OrderSpec:
    side: Side
    order_type: OrderType
    qty: float
    price: float | None
    stop_price: float | None = None
    take_profit: float | None = None
    reason: str = ""
    protective: bool = False
    level: float | None = None
    lot_buy_cids: list[str] = field(default_factory=list)


@dataclass
class GridPlan:
    regime: RegimeResult
    cancel: list[str] = field(default_factory=list)
    orders: list[OrderSpec] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    rebuild: bool = False
    stop_triggered: bool = False
    rejected_levels: list[str] = field(default_factory=list)


class GridStrategy:
    def __init__(self, cfg: GridStrategyConfig) -> None:
        self.cfg = cfg
        self.p = cfg.grid

    def plan(
        self,
        *,
        df: pd.DataFrame,
        book: OrderBook,
        now: datetime,
        st: GridSymbolState,
        position_qty: float,
        active: list[OrderRecord],
        market: MarketInfo,
        max_risk_usd: float,
        allow_new_buys: bool = True,
    ) -> GridPlan:
        p = self.p
        regime = classify_regime(df, self.cfg.regime, p.atr_period)
        plan = GridPlan(regime)
        mid = book.mid
        active_buys = {o.client_order_id: o for o in active if o.side == Side.BUY}
        active_sells = {o.client_order_id: o for o in active if o.side == Side.SELL}
        # forget buy orders that are no longer active
        for cid in list(st.buy_orders):
            if cid not in active_buys:
                st.buy_orders.pop(cid, None)
                st.buy_created.pop(cid, None)
        if mid is None or regime.atr is None:
            plan.reasons.append("no mid price or indicators unavailable: no new orders")
            return plan

        # ---- 1. protective grid stop
        if position_qty > EPS and st.stop_price and mid <= st.stop_price:
            plan.stop_triggered = True
            plan.cancel.extend(list(active_buys) + list(active_sells))
            qty = floor_to_step(position_qty, market.qty_step)
            if qty > 0:
                plan.orders.append(OrderSpec(Side.SELL, OrderType.MARKET, qty, None, protective=True,
                                             reason=f"grid stop {st.stop_price} hit at {mid:.2f}"))
            st.cooldown_until = (now + timedelta(minutes=p.stop_cooldown_minutes)).isoformat()
            st.reset_grid()
            st.stop_price = None
            plan.reasons.append("grid stop triggered: exiting inventory, cooldown started")
            return plan

        # ---- 2. take-profit sells for lots without an active sell
        self._plan_lot_sells(plan, st, active_sells, market, book)

        # ---- 3. regime / cooldown filters
        allowed = regime.regime.value in self.cfg.regime.allowed_regimes
        in_cooldown = bool(st.cooldown_until and now < datetime.fromisoformat(st.cooldown_until))
        if not allowed or in_cooldown or not allow_new_buys:
            plan.cancel.extend(cid for cid in active_buys if cid not in plan.cancel)
            why = (f"regime {regime.regime.value} not allowed" if not allowed
                   else "cooldown after grid stop" if in_cooldown else "new buys disabled")
            plan.reasons.append(f"{why}: canceling unfilled buys, no new positions")
            st.regime = regime.regime.value
            if regime.regime in (MarketRegime.STRONG_BEAR, MarketRegime.BEAR):
                st.reset_grid()
            return plan

        # ---- 4. rebuild decision
        spacing = adaptive_spacing(regime.atr_pct or 0.0, p)
        rebuild = st.center is None or st.spacing is None
        if not rebuild:
            drift = abs(mid - st.center) / st.center  # type: ignore[operator]
            if drift > p.rebuild_drift_spacings * st.spacing:  # type: ignore[operator]
                rebuild = True
                plan.reasons.append(f"price drifted {drift:.2%} from grid centre")
            elif st.atr and abs(regime.atr / st.atr - 1) > p.rebuild_atr_change_pct:
                rebuild = True
                plan.reasons.append("volatility changed: rebuilding grid")
            elif st.regime != regime.regime.value:
                rebuild = True
                plan.reasons.append(f"regime changed {st.regime} -> {regime.regime.value}")
        # stale orders
        max_age = timedelta(minutes=p.max_order_age_minutes)
        for cid, created in list(st.buy_created.items()):
            if now - datetime.fromisoformat(created) > max_age:
                plan.cancel.append(cid)
                st.buy_orders.pop(cid, None)
                st.buy_created.pop(cid, None)
                plan.reasons.append(f"stale buy order {cid} canceled")
        if rebuild:
            plan.rebuild = True
            plan.cancel.extend(cid for cid in active_buys if cid not in plan.cancel)
            st.buy_orders.clear()
            st.buy_created.clear()
            st.center, st.spacing, st.atr = mid, spacing, regime.atr
            st.regime = regime.regime.value
            st.built_at = now.isoformat()
        assert st.center is not None and st.spacing is not None and st.atr is not None
        levels = buy_levels(st.center, st.spacing, p.levels, market.price_step)
        if not levels:
            plan.reasons.append("no valid grid levels")
            return plan
        st.stop_price = grid_stop(levels[-1], st.atr, p.stop_atr_multiplier, market.price_step)

        # ---- 5. new buy levels
        pending_buy_value = sum(
            o.remaining * (o.price or 0) for cid, o in active_buys.items() if cid not in plan.cancel
        )
        inventory_value = position_qty * mid
        existing_levels = {round(px, 8) for cid, px in st.buy_orders.items() if cid not in plan.cancel}
        spread = book.spread_pct or 0.0
        for level in levels:
            if round(level, 8) in existing_levels:
                continue
            if level >= (book.best_ask or mid):
                continue  # would be a taker order; grid buys are maker-only
            qty = floor_to_step(p.level_notional_usd / level, market.qty_step)
            risk_per_unit = level - st.stop_price
            if risk_per_unit <= 0:
                continue
            qty = min(qty, floor_to_step(max_risk_usd / risk_per_unit, market.qty_step))
            notional = qty * level
            if qty < market.min_qty or notional < market.min_notional or qty <= 0:
                plan.rejected_levels.append(f"{level}: size below venue minimum after risk cap")
                continue
            if inventory_value + pending_buy_value + notional > p.max_inventory_usd_per_symbol + 1e-9:
                plan.rejected_levels.append(f"{level}: inventory cap {p.max_inventory_usd_per_symbol} reached")
                continue
            target = sell_target(level, st.spacing, market.price_step)
            np_ = expected_net_profit(level, target, qty, market.maker_fee, spread, p.adverse_spread_buffer)
            if np_.net < p.min_net_profit_usd or np_.net_pct < p.min_net_profit_pct:
                plan.rejected_levels.append(
                    f"{level}: expected net {np_.net:.4f} USD ({np_.net_pct:.3%}) below minimum")
                continue
            plan.orders.append(OrderSpec(
                Side.BUY, OrderType.LIMIT, qty, level, stop_price=st.stop_price, take_profit=target, level=level,
                reason=(f"grid buy L={level} spacing={st.spacing:.3%} regime={regime.regime.value} "
                        f"exp_net={np_.net:.3f}USD"),
            ))
            pending_buy_value += notional
        return plan

    def _plan_lot_sells(self, plan: GridPlan, st: GridSymbolState, active_sells: dict[str, OrderRecord],
                        market: MarketInfo, book: OrderBook) -> None:
        spacing = st.spacing or self.p.min_spacing_pct
        uncovered = [lot_ for lot_ in st.lots if lot_.qty > EPS and (lot_.sell_cid is None or lot_.sell_cid not in active_sells)]
        if not uncovered:
            return
        bid = book.best_bid or 0.0
        dust: list[Lot] = []
        for lot in uncovered:
            target = lot.target or sell_target(lot.price, spacing, market.price_step)
            price = max(target, bid)
            qty = floor_to_step(lot.qty, market.qty_step)
            if qty * price < market.min_notional or qty < market.min_qty:
                dust.append(lot)
                continue
            plan.orders.append(OrderSpec(Side.SELL, OrderType.LIMIT, qty, price, reason=f"grid take-profit for buy @{lot.price}",
                                         lot_buy_cids=[lot.buy_cid]))
        if dust:
            qty = floor_to_step(sum(lot_.qty for lot_ in dust), market.qty_step)
            target = max(sell_target(max(lot_.price for lot_ in dust), spacing, market.price_step), bid)
            if qty > 0 and qty * target >= market.min_notional and qty >= market.min_qty:
                plan.orders.append(OrderSpec(Side.SELL, OrderType.LIMIT, qty, target,
                                             reason="grid take-profit (merged small lots)",
                                             lot_buy_cids=[lot_.buy_cid for lot_ in dust]))
