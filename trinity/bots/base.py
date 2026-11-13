"""Common bot infrastructure: health, persisted state, safe stepping."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from trinity.core.clock import Clock, RealClock
from trinity.core.log import get_logger
from trinity.core.models import BotStatus, Mode
from trinity.core.order_gateway import OrderGateway
from trinity.database.repository import Repository
from trinity.exchanges.base_exchange import ExchangeUnavailable, MarketDataSource, NotConfigured
from trinity.notifications.notifier import LogNotifier, Notifier


class BaseBot(ABC):
    bot_type: str = "base"

    def __init__(
        self,
        *,
        strategy_id: str,
        gateway: OrderGateway | None,
        market: MarketDataSource | None,
        repo: Repository,
        mode: Mode,
        clock: Clock | None = None,
        notifier: Notifier | None = None,
        loop_interval: float = 30.0,
    ) -> None:
        self.strategy_id = strategy_id
        self.gateway = gateway
        self.market = market
        self.repo = repo
        self.mode = mode
        self.clock = clock or RealClock()
        self.notifier = notifier or LogNotifier()
        self.loop_interval = loop_interval
        self.log = get_logger(f"trinity.bot.{strategy_id}")
        self.running = False
        self.paused = False
        self.last_error: str | None = None
        self.last_step_at: datetime | None = None
        self.consecutive_errors = 0
        self.connection_error = False
        self.steps = 0
        self.info: dict[str, Any] = {}

    # ------------------------------------------------------------------ state
    @property
    def state_key(self) -> str:
        return f"bot:{self.mode.value}:{self.strategy_id}:state"

    def load_state(self) -> dict[str, Any]:
        return self.repo.get_kv(self.state_key, {}) or {}

    def save_state(self, state: dict[str, Any]) -> None:
        self.repo.set_kv(self.state_key, state)

    # ------------------------------------------------------------------ status
    def status(self) -> BotStatus:
        if not self.running:
            return BotStatus.STOPPED
        if self.market is None:
            return BotStatus.NO_DATA
        if self.connection_error:
            return BotStatus.CONNECTION_ERROR
        if self.gateway is not None and self.gateway.risk_locked():
            locks = self.gateway.risk_locked()
            if locks != ["paused"]:
                return BotStatus.RISK_LOCKED
            return BotStatus.PAUSED
        if self.paused:
            return BotStatus.PAUSED
        return BotStatus.LIVE_MODE if self.mode == Mode.LIVE else BotStatus.PAPER_MODE

    def describe(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "type": self.bot_type,
            "status": self.status().value,
            "mode": self.mode.value,
            "last_step": self.last_step_at.isoformat() if self.last_step_at else None,
            "last_error": self.last_error,
            "steps": self.steps,
            **self.info,
        }

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False

    @abstractmethod
    async def step(self) -> None:
        """One decision cycle. Must be idempotent and safe to call repeatedly."""

    async def safe_step(self) -> bool:
        """Run one step, converting recoverable errors into status. Returns success."""
        # NOTE: "paused" only disables NEW entries (checked inside each bot). The step keeps running so
        # open positions stay managed (stops, take profits, exits) - SPEC_4.0 A2/A3.
        if not self.running:
            return True
        try:
            await self.step()
            self.consecutive_errors = 0
            self.connection_error = False
            self.last_error = None
            return True
        except (ExchangeUnavailable, NotConfigured) as exc:
            self.consecutive_errors += 1
            self.connection_error = True
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.log.warning("connection problem: %s", exc)
            return False
        finally:
            self.steps += 1
            self.last_step_at = self.clock.now()
            self.repo.upsert_bot_health(
                self.strategy_id, self.status().value, self.mode, self.last_error, 0,
                {"steps": self.steps, **{k: v for k, v in self.info.items() if _jsonable(v)}},
                ts=self.clock.now(),
            )


def _jsonable(v: Any) -> bool:
    return isinstance(v, str | int | float | bool | type(None) | dict | list)
