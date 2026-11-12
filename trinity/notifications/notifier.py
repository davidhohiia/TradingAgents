"""Notification interface (Telegram, log, fan-out)."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod

from trinity.core.log import get_logger, redact

log = get_logger(__name__)


class Notifier(ABC):
    @abstractmethod
    async def notify(self, event_type: str, text: str, severity: str = "INFO") -> None: ...


class LogNotifier(Notifier):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def notify(self, event_type: str, text: str, severity: str = "INFO") -> None:
        self.sent.append((event_type, redact(text), severity))
        log.info("[notify:%s] %s", event_type, redact(text))


class MultiNotifier(Notifier):
    def __init__(self, *notifiers: Notifier) -> None:
        self.notifiers = list(notifiers)

    def add(self, n: Notifier) -> None:
        self.notifiers.append(n)

    async def notify(self, event_type: str, text: str, severity: str = "INFO") -> None:
        results = await asyncio.gather(
            *(n.notify(event_type, text, severity) for n in self.notifiers), return_exceptions=True
        )
        for r in results:
            if isinstance(r, Exception):
                log.warning("notifier failed: %s", r)
