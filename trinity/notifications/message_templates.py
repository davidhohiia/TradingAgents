"""Telegram / report message templates. Never include secrets here."""

from __future__ import annotations

from typing import Any

MODE_BANNER = {
    "paper": "🧪 PAPER MODE — virtual trading, results are NOT real money",
    "live": "🔴 LIVE MODE — real money",
}

HELP = (
    "TRINITY 3.0 commands:\n"
    "/status /balance /pnl /positions /trades\n"
    "/crypto (/grid) /stocks /arbitrage /risk /report\n"
    "/capital /strategies\n"
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
             f"Equity: {money(d['equity'])} (contributed {money(d.get('contributed'))})",
             f"Cash (not invested): {money(d['cash'])}",
             f"Invested (cost): {money(d['invested'])}", f"Reserve (untouchable): {money(d['reserve'])}"]
    for sid, c in d["strategies"].items():
        lines.append(f"• {sid}: alloc {money(c['allocation'])}, deployed {money(c['deployed'])}, "
                     f"reserved {money(c['reserved'])}, available {money(c['available'])}")
    return "\n".join(lines)


def pnl(d: dict[str, Any]) -> str:
    return "\n".join([
        MODE_BANNER.get(d["mode"], d["mode"]),
        f"Realized: {money(d['realized'])}", f"Unrealized: {money(d['unrealized'])}",
        f"Today: {money(d['daily_pnl'])}",
        f"Trading profit (excl. deposits): {money(d['total_pnl'])} on contributed {money(d.get('contributed'))}",
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
        f"Weekly PnL {money(d.get('weekly_pnl'))} / limit -{money(d.get('max_weekly_loss_usd'))}",
        f"Drawdown {pct(d.get('drawdown_pct'))} | tiers: warn {pct(d['drawdown_tiers']['warning'])}, "
        f"reduce {pct(d['drawdown_tiers']['reduce'])}, lock {pct(d['drawdown_tiers']['live_lock'])}, "
        f"hard stop {pct(d['drawdown_tiers']['hard_stop'])}",
        f"Activity multiplier x{d.get('activity_multiplier', 1)} | risk/trade max {money(d.get('max_risk_per_trade_usd'))}"
        f" | open risk {money(d.get('open_risk'))} / {money(d.get('max_total_open_risk_usd'))}",
        f"Leverage: {'ENABLED (gated)' if d.get('leverage_enabled') else 'OFF'}",
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
             f"Day PnL {money(d['daily_pnl'])} | Week PnL {money(d.get('weekly_pnl'))} | Max DD {pct(d['max_drawdown'])}",
             f"Contributed {money(d.get('contributed'))} | trading profit (excl. deposits) {money(d.get('trading_profit'))}",
             f"Trades today: {d['trades_today']} (wins {d['wins']}, losses {d['losses']}) fees {money(d['fees'])}"]
    for sid, s in d.get("by_strategy", {}).items():
        lines.append(f"• {sid}: {s['trades']} trades, PnL {money(s['pnl'])}")
    if d.get("recommendations"):
        lines.append("Advisory (not applied automatically):")
        lines.extend(f"  - {r}" for r in d["recommendations"][:6])
    lines.append("Past results do not guarantee future returns.")
    return "\n".join(lines)


def capital(d: dict[str, Any]) -> str:
    lines = [MODE_BANNER.get(d["mode"], d["mode"]),
             f"Contributed capital: {money(d['contributed'])} ({len(d['deposits'])} deposits)",
             f"Equity: {money(d['equity'])} | trading profit {money(d['trading_profit'])} "
             f"({pct(d['return_on_contributed'])} on contributed) | fees {money(d['fees_paid'])}",
             f"Reserve (never used automatically): {money(d['reserve'])}"]
    lines += [f"• {k}: {money(v)}" for k, v in d["allocations"].items()]
    for m in d.get("monthly", [])[-3:]:
        lines.append(f"{m['month']}: return {pct(m['return'])} (deposits {money(m['deposits'])}) "
                     f"— info target {pct(m['target_informational'])}, NOT a trading goal")
    if d.get("pending_proposals"):
        lines.append("Pending reallocation proposals (approve locally with scripts/capital.py):")
        lines += [f"  #{p['id']} {p['strategy_id']} +{money(p['amount_usd'])}: {p['rationale']}"
                  for p in d["pending_proposals"]]
    return "\n".join(lines)


def strategies(d: dict[str, Any]) -> str:
    lines = [MODE_BANNER.get(d["mode"], d["mode"]), "Per-strategy statistics:"]
    for k, s in d["stats"].items():
        wr = "n/a" if s["win_rate"] is None else f"{s['win_rate']:.0%}"
        pf = "n/a" if s["profit_factor"] is None else f"{s['profit_factor']:.2f}"
        lines.append(f"• {k}: {s['trades']} trades, net {money(s['net_pnl'])}, win {wr}, PF {pf}")
    for sid, rv in d["real_vs_virtual"].items():
        lines.append(f"{sid}: paper net {money(rv['paper_virtual']['net_pnl'])} vs live net "
                     f"{money(rv['live_real']['net_pnl'])} {'; '.join(rv['flags'])}")
    if d["degraded"]:
        lines.append("Degradation: " + "; ".join(f"{k}: {', '.join(v)}" for k, v in d["degraded"].items()))
    if d["strategy_correlation"]:
        lines.append("Strategy PnL correlation: " + ", ".join(f"{k} {v:.2f}" for k, v in d["strategy_correlation"].items()))
    return "\n".join(lines)
