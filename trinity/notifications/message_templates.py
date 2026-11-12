"""Telegram / report message templates. Never include secrets here."""

from __future__ import annotations

from typing import Any

MODE_BANNER = {
    "paper": "🧪 PAPER MODE — virtual trading, results are NOT real money",
    "live": "🔴 LIVE MODE — real money",
}

HELP = (
    "TRINITY commands:\n"
    "/status /balance /pnl /positions /trades\n"
    "/grid /stocks /arbitrage /risk /report\n"
    "/pause /resume /stop /emergency_stop\n"
    "/paper /live_status"
)


def money(x: float | None) -> str:
    return "n/a" if x is None else f"${x:,.2f}"


def pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.2%}"


def status(d: dict[str, Any]) -> str:
    lines = [MODE_BANNER.get(d["mode"], d["mode"]), f"Trading locks: {', '.join(d['locks']) or 'none'}"]
    for b in d["bots"]:
        lines.append(f"• {b['strategy_id']}: {b['status']}" + (f" — {b['last_error']}" if b.get("last_error") else ""))
    lines.append(f"Equity {money(d.get('equity'))} | day PnL {money(d.get('daily_pnl'))} | DD {pct(d.get('drawdown'))}")
    return "\n".join(lines)


def balance(d: dict[str, Any]) -> str:
    lines = [MODE_BANNER.get(d["mode"], d["mode"]),
             f"Equity: {money(d['equity'])}", f"Cash (not invested): {money(d['cash'])}",
             f"Invested (cost): {money(d['invested'])}", f"Reserve (untouchable): {money(d['reserve'])}"]
    for sid, c in d["strategies"].items():
        lines.append(f"• {sid}: alloc {money(c['allocation'])}, deployed {money(c['deployed'])}, "
                     f"reserved {money(c['reserved'])}, available {money(c['available'])}")
    return "\n".join(lines)


def pnl(d: dict[str, Any]) -> str:
    return "\n".join([
        MODE_BANNER.get(d["mode"], d["mode"]),
        f"Realized: {money(d['realized'])}", f"Unrealized: {money(d['unrealized'])}",
        f"Today: {money(d['daily_pnl'])}", f"Total vs start: {money(d['total_pnl'])}",
        f"Fees paid: {money(d['fees'])}", f"Closed trades: {d['trades']}",
    ])


def positions(rows: list[dict[str, Any]], mode: str) -> str:
    if not rows:
        return f"[{mode.upper()}] No open positions."
    out = [f"[{mode.upper()}] Open positions:"]
    for r in rows:
        out.append(f"• {r['strategy_id']} {r['symbol']}: {r['quantity']:g} @ {r['avg']:.2f} "
                   f"last {r['last'] if r['last'] is None else round(r['last'], 2)} uPnL {money(r['upnl'])}"
                   + (f" stop {r['stop']:.2f}" if r.get("stop") else ""))
    return "\n".join(out)


def trades(rows: list[dict[str, Any]], mode: str) -> str:
    if not rows:
        return f"[{mode.upper()}] No closed trades yet."
    out = [f"[{mode.upper()}] Last trades:"]
    for r in rows:
        out.append(f"• {r['exit_time'][:16]} {r['strategy_id']} {r['symbol']} {r['quantity']:g} "
                   f"{r['entry_price']:.2f}→{r['exit_price']:.2f} PnL {money(r['realized_pnl'])}")
    return "\n".join(out)


def risk(d: dict[str, Any]) -> str:
    breakers = ", ".join(f"{k}={v['state']}" for k, v in d.get("breakers", {}).items()) or "all ok"
    return "\n".join([
        MODE_BANNER.get(d["mode"], d["mode"]),
        f"Locks: {', '.join(d['locks']) or 'none'}",
        f"Daily PnL {money(d.get('daily_pnl'))} / limit -{money(d['max_daily_loss_usd'])}",
        f"Drawdown {pct(d.get('drawdown_pct'))} / limit {pct(d['max_drawdown_pct'])}",
        f"Exposure {money(d.get('exposure'))} / max {money(d['max_total_exposure_usd'])}",
        f"API breakers: {breakers}",
    ])


def bot_detail(d: dict[str, Any]) -> str:
    lines = [f"{d['strategy_id']} — {d['status']} ({d['mode']})", f"last step: {d.get('last_step')}"]
    for k, v in d.items():
        if k in ("strategy_id", "status", "mode", "last_step", "type"):
            continue
        lines.append(f"{k}: {str(v)[:700]}")
    return "\n".join(lines)


def daily_report(d: dict[str, Any]) -> str:
    lines = [f"📊 Daily report {d['date']} — {MODE_BANNER.get(d['mode'], d['mode'])}",
             f"Equity {money(d['equity'])} (start of day {money(d['day_start'])})",
             f"Day PnL {money(d['daily_pnl'])} | Max DD {pct(d['max_drawdown'])}",
             f"Trades today: {d['trades_today']} (wins {d['wins']}, losses {d['losses']}) fees {money(d['fees'])}"]
    for sid, s in d.get("by_strategy", {}).items():
        lines.append(f"• {sid}: {s['trades']} trades, PnL {money(s['pnl'])}")
    if d.get("recommendations"):
        lines.append("Advisory (not applied automatically):")
        lines.extend(f"  - {r}" for r in d["recommendations"][:6])
    lines.append("Past results do not guarantee future returns.")
    return "\n".join(lines)
