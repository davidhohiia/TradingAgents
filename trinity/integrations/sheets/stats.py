"""Daily / bot / portfolio statistics for the journal (pure functions over confirmed records)."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from trinity.integrations.sheets.journal import BOT_NAMES, JOURNAL_TZ, local_date_time

DAILY_HEADER = ["Дата", "Закрытых сделок", "Прибыль (USD)", "Убыток (USD)", "Чистый результат (USD)",
                "Доходность за день (%)", "Баланс портфеля (USD)"]
BOT_HEADER = ["Название бота / стратегия", "Всего сделок", "Прибыльных", "Убыточных", "Win Rate (%)",
              "Средняя прибыль (USD)", "Средний убыток (USD)", "Общая прибыль (USD)", "Общий убыток (USD)",
              "Чистый результат (USD)", "Чистая доходность (%)"]


def daily_stats(trades: list[dict[str, Any]], equity_points: list[tuple[datetime, float]],
                tz: str = JOURNAL_TZ) -> list[list[Any]]:
    """One row per local (Amsterdam) day with closed trades. Balance/return only from recorded equity snapshots."""
    zone = ZoneInfo(tz)
    by_day: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        by_day[local_date_time(t["exit_time"], tz)[0]].append(float(t["net_pnl"]))
    eq_by_day: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for ts, eq in sorted(equity_points):
        eq_by_day[ts.astimezone(zone).strftime("%Y-%m-%d")].append((ts, eq))
    ordered_eq = sorted(equity_points)
    rows = []
    for day in sorted(by_day):
        pnls = by_day[day]
        profit = sum(p for p in pnls if p > 0)
        loss = sum(p for p in pnls if p < 0)
        net = profit + loss
        start_eq = None
        for ts, eq in ordered_eq:  # last snapshot before the local day starts, else first of the day
            if ts.astimezone(zone).strftime("%Y-%m-%d") < day:
                start_eq = eq
        if start_eq is None and eq_by_day.get(day):
            start_eq = eq_by_day[day][0][1]
        end_eq = eq_by_day[day][-1][1] if eq_by_day.get(day) else ""
        ret = net / start_eq * 100.0 if start_eq else ""
        rows.append([day, len(pnls), profit, loss, net, ret, end_eq])
    return rows


def _summary(label: str, nets: list[float], base: float | None) -> list[Any]:
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    n = len(nets)
    net = sum(nets)
    return [label, n, len(wins), len(losses), (len(wins) / n * 100.0) if n else "",
            (sum(wins) / len(wins)) if wins else "", (sum(losses) / len(losses)) if losses else "",
            sum(wins), sum(losses), net, (net / base * 100.0) if base else ""]


def bot_stats(trades: list[dict[str, Any]], allocations: dict[str, float]) -> list[list[Any]]:
    """Per bot, then per sub-strategy. Net return % is relative to the bot's allocated capital."""
    rows = []
    bots = sorted({t["strategy_id"] for t in trades} | {k for k, v in allocations.items() if v > 0})
    for sid in bots:
        nets = [float(t["net_pnl"]) for t in trades if t["strategy_id"] == sid]
        rows.append(_summary(BOT_NAMES.get(sid, sid), nets, allocations.get(sid)))
        for sub in sorted({t["sub_strategy"] for t in trades if t["strategy_id"] == sid and t["sub_strategy"]}):
            sub_nets = [float(t["net_pnl"]) for t in trades if t["strategy_id"] == sid and t["sub_strategy"] == sub]
            rows.append(_summary(f"{BOT_NAMES.get(sid, sid)} / {sub}", sub_nets, None))
    return rows


def portfolio_rows(p: dict[str, Any], tz: str = JOURNAL_TZ) -> list[list[Any]]:
    contributed = p["contributed"]
    now = datetime.fromisoformat(p["ts"]).astimezone(ZoneInfo(tz))
    return [
        ["Показатель", "Значение"],
        ["Режим", "LIVE (реальные деньги)" if p["mode"] == "live" else "PAPER (виртуальная торговля)"],
        ["Начальный депозит (USD)", p["initial_deposit"]],
        ["Дополнительные пополнения (USD)", p["additional_deposits"]],
        ["Текущий баланс (USD)", p["equity"]],
        ["Свободные средства (USD)", p["cash"]],
        ["Средства в открытых позициях (USD)", p["in_positions"]],
        ["Общая прибыль/убыток (USD, без пополнений)", p["equity"] - contributed],
        ["Доходность (%)", (p["equity"] - contributed) / contributed * 100.0 if contributed else ""],
        ["Текущая просадка (%)", p["drawdown_pct"] * 100.0 if p.get("drawdown_pct") is not None else ""],
        ["Резерв (USD)", p.get("reserve", "")],
        [f"Обновлено ({tz})", now.strftime("%Y-%m-%d %H:%M:%S")],
    ]
