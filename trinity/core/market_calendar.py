"""US equity trading session calendar (fallback when the broker clock API is unavailable)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from trinity.config.settings import SessionConfig


class UsEquityCalendar:
    def __init__(self, cfg: SessionConfig) -> None:
        self.tz = ZoneInfo(cfg.timezone)
        self.open_t = time.fromisoformat(cfg.open)
        self.close_t = time.fromisoformat(cfg.close)
        self.early_close_t = time.fromisoformat(cfg.early_close)
        self.holidays = {date.fromisoformat(d) for d in cfg.holidays}
        self.early_closes = {date.fromisoformat(d) for d in cfg.early_closes}

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def session(self, d: date) -> tuple[datetime, datetime] | None:
        if not self.is_trading_day(d):
            return None
        close_t = self.early_close_t if d in self.early_closes else self.close_t
        return (datetime.combine(d, self.open_t, self.tz), datetime.combine(d, close_t, self.tz))

    def is_open(self, now: datetime) -> bool:
        local = now.astimezone(self.tz)
        sess = self.session(local.date())
        return bool(sess and sess[0] <= local < sess[1])

    def minutes_to_close(self, now: datetime) -> float | None:
        local = now.astimezone(self.tz)
        sess = self.session(local.date())
        if not sess or not (sess[0] <= local < sess[1]):
            return None
        return (sess[1] - local).total_seconds() / 60

    def next_open(self, now: datetime) -> datetime:
        local = now.astimezone(self.tz)
        d = local.date()
        for _ in range(15):
            sess = self.session(d)
            if sess and local < sess[0]:
                return sess[0]
            d += timedelta(days=1)
            local = datetime.combine(d, time(0, 0), self.tz)
        raise RuntimeError("no session found in the next 15 days")
