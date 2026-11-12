"""Equity peak / drawdown / daily and weekly PnL tracking.

State is persisted (restarts never reset it). Deposits shift every baseline by the
deposited amount (CapitalGrowthManager.register_deposit), so contributions are never
counted as performance and never hide a drawdown.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from trinity.core.clock import utc_day
from trinity.core.models import Mode
from trinity.database.repository import Repository


def iso_week(ts: datetime) -> str:
    y, w, _ = ts.isocalendar()
    return f"{y}-W{w:02d}"


@dataclass(frozen=True)
class DrawdownStatus:
    equity: float
    peak_equity: float
    drawdown_pct: float
    max_drawdown_pct: float
    day: str
    day_start_equity: float
    daily_pnl: float
    week: str
    week_start_equity: float
    weekly_pnl: float

    @property
    def daily_pnl_pct(self) -> float:
        return self.daily_pnl / self.day_start_equity if self.day_start_equity > 0 else 0.0

    @property
    def weekly_pnl_pct(self) -> float:
        return self.weekly_pnl / self.week_start_equity if self.week_start_equity > 0 else 0.0


class DrawdownMonitor:
    def __init__(self, repo: Repository, mode: Mode, initial_capital: float) -> None:
        self.repo = repo
        self.mode = mode
        self.initial_capital = initial_capital
        self._key = f"risk:{mode.value}:equity_state"

    def _load(self) -> dict[str, float | str]:
        state = self.repo.get_kv(self._key)
        if not state:
            state = {"peak": self.initial_capital, "max_dd": 0.0, "day": "", "day_start": self.initial_capital,
                     "week": "", "week_start": self.initial_capital}
        state.setdefault("week", "")
        state.setdefault("week_start", state.get("day_start", self.initial_capital))
        return state

    def peek(self) -> dict[str, float | str]:
        return self._load()

    def update(self, equity: float, now: datetime) -> DrawdownStatus:
        state = self._load()
        day, week = utc_day(now), iso_week(now)
        if state["day"] != day:
            state["day"] = day
            state["day_start"] = equity
        if state["week"] != week:
            state["week"] = week
            state["week_start"] = equity
        peak = max(float(state["peak"]), equity)
        dd = 0.0 if peak <= 0 else max(0.0, (peak - equity) / peak)
        state["peak"] = peak
        state["max_dd"] = max(float(state["max_dd"]), dd)
        self.repo.set_kv(self._key, state)
        return DrawdownStatus(
            equity=equity, peak_equity=peak, drawdown_pct=dd, max_drawdown_pct=float(state["max_dd"]),
            day=day, day_start_equity=float(state["day_start"]), daily_pnl=equity - float(state["day_start"]),
            week=week, week_start_equity=float(state["week_start"]), weekly_pnl=equity - float(state["week_start"]),
        )

    def reset_peak(self, equity: float) -> None:
        """Used only after a MANUAL drawdown-lock confirmation."""
        state = self._load()
        state["peak"] = equity
        self.repo.set_kv(self._key, state)
