"""TRINITY dashboard (Streamlit, read-only access to the database).

    streamlit run trinity/dashboard/app.py

Paper (virtual) and live (real) results are NEVER mixed: every view is filtered by mode,
and paper views carry a permanent "PAPER MODE — virtual" banner.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from trinity.config.settings import load_app_config, load_settings  # noqa: E402
from trinity.core.models import Mode  # noqa: E402
from trinity.database.repository import Repository  # noqa: E402

STATUS_COLORS = {
    "PAPER MODE": "#2b7bb9", "LIVE MODE": "#c0392b", "STOPPED": "#7f8c8d", "PAUSED": "#d68910",
    "RISK LOCKED": "#8e44ad", "CONNECTION ERROR": "#e67e22", "SCAN ONLY (VIRTUAL)": "#16a085",
    "NO DATA SOURCE": "#7f8c8d", "ERROR": "#c0392b",
}


def badge(text: str) -> str:
    color = STATUS_COLORS.get(text, "#555")
    return (f"<span style='background:{color};color:white;padding:3px 10px;border-radius:6px;"
            f"font-weight:600'>{text}</span>")


@st.cache_resource
def get_repo(path: str) -> Repository | None:
    if not Path(path).exists():
        return None
    return Repository(Path(path), read_only=True)


def df_rows(rows) -> pd.DataFrame:  # noqa: ANN001
    return pd.DataFrame([dict(r) for r in rows])


def main() -> None:
    st.set_page_config(page_title="TRINITY", layout="wide")
    settings = load_settings()
    config = load_app_config()
    repo = get_repo(str(settings.database_path))
    st.sidebar.title("TRINITY")
    if repo is None:
        st.error(f"Database not found: {settings.database_path}. Start the engine first (run_paper).")
        return
    mode = st.sidebar.radio("Mode", ["paper", "live"], index=0,
                            help="Paper (virtual) and live (real) data are always shown separately.")
    page = st.sidebar.radio("Page", ["Overview", "Crypto Momentum", "Stock Trend", "Arbitrage", "Capital",
                                     "Strategies", "Risk", "System"])
    st.sidebar.button("Refresh")
    if mode == "paper":
        st.info("🧪 PAPER MODE — all figures are VIRTUAL (simulated execution). They are not real money results.")
    else:
        st.error("🔴 LIVE MODE view — real money accounts.")
    if page == "Overview":
        overview(repo, config, mode)
    elif page == "Crypto Momentum":
        bot_page(repo, "crypto_momentum", mode)
    elif page == "Capital":
        capital_page(repo, config, mode)
    elif page == "Strategies":
        strategies_page(repo, mode)
    elif page == "Stock Trend":
        bot_page(repo, "stock_trend", mode)
    elif page == "Arbitrage":
        arbitrage_page(repo)
    elif page == "Risk":
        risk_page(repo, config, mode)
    else:
        system_page(repo)


def overview(repo: Repository, config, mode: str) -> None:  # noqa: ANN001
    eq = df_rows(repo.query("SELECT * FROM equity_history WHERE mode=? ORDER BY id", (mode,)))
    pos = df_rows(repo.query("SELECT * FROM positions WHERE mode=? AND quantity>1e-12", (mode,)))
    contributed = repo.query("SELECT COALESCE(SUM(amount_usd), 0) FROM deposits")[0][0] or config.capital.total_capital_usd
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    if not eq.empty:
        last = eq.iloc[-1]
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        dm = repo.query("SELECT start_equity FROM daily_metrics WHERE date=? AND mode=? AND strategy_id='ALL'",
                        (today, mode))
        day_start = dm[0][0] if dm and dm[0][0] is not None else last["equity"]
        c1.metric("Portfolio value", f"${last['equity']:,.2f}")
        c2.metric("Cash (not invested)", f"${last['cash']:,.2f}")
        c3.metric("Invested", f"${last['invested']:,.2f}")
        c4.metric("Day PnL", f"${last['equity'] - day_start:+,.2f}")
        c5.metric("Trading profit (excl. deposits)", f"${last['equity'] - contributed:+,.2f}")
        peak = eq["equity"].cummax()
        c6.metric("Max drawdown", f"{((peak - eq['equity']) / peak).max():.2%}")
        st.subheader("Equity curve" + (" (VIRTUAL)" if mode == "paper" else ""))
        chart = eq.assign(ts=pd.to_datetime(eq["ts"])).set_index("ts")[["equity"]]
        st.line_chart(chart)
    else:
        st.write("No equity history yet.")
    st.subheader("Bots")
    health = df_rows(repo.list_bot_health())
    if not health.empty:
        for _, r in health.iterrows():
            st.markdown(f"**{r['strategy_id']}** {badge(r['status'])} &nbsp; last heartbeat {r['last_heartbeat']}"
                        + (f" — `{r['last_error']}`" if r["last_error"] else ""), unsafe_allow_html=True)
    st.subheader("Capital allocation")
    state = repo.get_kv("capital:allocations") or {"strategies": dict(config.capital.allocations),
                                                    "reserve": config.capital.reserve_usd}
    alloc = dict(state["strategies"])
    alloc["reserve (never auto-used)"] = state["reserve"]
    st.bar_chart(pd.Series(alloc, name="USD"))
    st.subheader("Open positions")
    st.dataframe(pos[["strategy_id", "symbol", "quantity", "avg_entry_price", "last_price", "unrealized_pnl",
                      "stop_price"]] if not pos.empty else pos, use_container_width=True)
    st.subheader("Active orders")
    orders = df_rows(repo.query("SELECT client_order_id, strategy_id, symbol, side, order_type, quantity, price, "
                                "filled_quantity, status, created_at FROM orders WHERE mode=? AND status IN "
                                "('pending_submit','open','partially_filled') ORDER BY id DESC", (mode,)))
    st.dataframe(orders, use_container_width=True)
    st.subheader("Recent trades")
    st.dataframe(df_rows(repo.list_trades(Mode(mode), 30)), use_container_width=True)


def bot_page(repo: Repository, sid: str, mode: str) -> None:
    st.header(sid)
    h = repo.query("SELECT * FROM bot_health WHERE strategy_id=?", (sid,))
    if h:
        r = h[0]
        st.markdown(badge(r["status"]), unsafe_allow_html=True)
        st.write(f"last heartbeat: {r['last_heartbeat']} | restarts: {r['restarts']}")
        if r["last_error"]:
            st.warning(r["last_error"])
        st.json(json.loads(r["details_json"] or "{}"), expanded=False)
    stats = repo.trade_stats(Mode(mode), sid)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Closed trades", int(stats["n"]))
    c2.metric("Net PnL" + (" (virtual)" if mode == "paper" else ""), f"${stats['pnl']:+.2f}")
    c3.metric("Win rate", f"{stats['wins'] / stats['n']:.0%}" if stats["n"] else "n/a")
    c4.metric("Fees", f"${stats['fees']:.2f}")
    st.subheader("Positions")
    st.dataframe(df_rows(repo.query("SELECT * FROM positions WHERE strategy_id=? AND mode=?", (sid, mode))),
                 use_container_width=True)
    st.subheader("Orders")
    st.dataframe(df_rows(repo.query("SELECT client_order_id, symbol, side, order_type, quantity, price, "
                                    "filled_quantity, avg_fill_price, fee_usd, status, reason, created_at FROM orders "
                                    "WHERE strategy_id=? AND mode=? ORDER BY id DESC LIMIT 200", (sid, mode))),
                 use_container_width=True)
    st.subheader("Trades")
    st.dataframe(df_rows(repo.list_trades(Mode(mode), 200, sid)), use_container_width=True)


def arbitrage_page(repo: Repository) -> None:
    st.header("Arbitrage — SCAN ONLY / VIRTUAL")
    st.warning("No real arbitrage orders are ever placed. 'virtual_executed' rows are simulations.")
    opps = df_rows(repo.list_arbitrage_opportunities(500))
    if opps.empty:
        st.write("No scans recorded yet.")
        return
    acc = opps[opps["accepted"] == 1]
    c1, c2, c3 = st.columns(3)
    c1.metric("Recorded evaluations", len(opps))
    c2.metric("Accepted (virtual)", len(acc))
    c3.metric("Virtual net (accepted)", f"${acc['net_profit'].sum():+.2f}" if not acc.empty else "$0.00")
    st.dataframe(opps, use_container_width=True)


def risk_page(repo: Repository, config, mode: str) -> None:  # noqa: ANN001
    st.header("Risk")
    state = repo.get_kv(f"risk:{mode}:state") or {}
    locks = [k for k in ("emergency_stop", "hard_stop", "recovery_lock", "drawdown_lock", "paused") if state.get(k)]
    if state.get("daily_lock_day") == datetime.now(UTC).strftime("%Y-%m-%d"):
        locks.append("daily_loss_lock")
    st.markdown(badge("RISK LOCKED") if locks else badge("PAPER MODE" if mode == "paper" else "LIVE MODE"),
                unsafe_allow_html=True)
    st.write("Active locks:", locks or "none", state.get("reasons", {}))
    r = config.risk
    eqs = repo.get_kv(f"risk:{mode}:equity_state") or {}
    peak = eqs.get("peak")
    st.write(f"Equity peak (deposit-adjusted): {peak}  |  max drawdown seen: {eqs.get('max_dd', 0):.2%}")
    st.table(pd.DataFrame({
        "limit": ["risk per trade", "total open risk", "daily loss", "weekly loss", "DD warning", "DD reduced activity",
                  "DD live lock (manual confirm)", "DD hard stop (absolute)", "max total exposure", "max order",
                  "leverage"],
        "value": [f"{r.max_risk_per_trade_pct:.2%} of equity", f"{r.max_total_open_risk_pct:.0%} of equity",
                  f"{r.max_daily_loss_pct:.0%} of day-start equity", f"{r.max_weekly_loss_pct:.0%} of week-start equity",
                  f"{r.drawdown_warning_pct:.0%}", f"{r.drawdown_reduce_pct:.0%} (risk x{r.reduced_activity_multiplier})",
                  f"{r.drawdown_live_lock_pct:.0%}", f"{r.drawdown_emergency_pct:.0%}",
                  f"{r.max_total_exposure_pct:.0%} of equity", f"${r.max_order_notional_usd:.0f}",
                  "OFF" if not config.leverage.enabled else f"gated, isolated, max {config.leverage.max_leverage}x"]}))
    st.subheader("Risk events")
    ev = df_rows(repo.query("SELECT ts, severity, event_type, strategy_id, symbol, message FROM risk_events "
                            "WHERE mode=? ORDER BY id DESC LIMIT 300", (mode,)))
    st.dataframe(ev, use_container_width=True)


def capital_page(repo: Repository, config, mode: str) -> None:  # noqa: ANN001
    st.header("Capital growth")
    st.caption(f"Informational monthly target {config.capital.informational_monthly_target_pct:.0%} — "
               "NOT a trading goal, never used to open trades. No return is guaranteed.")
    deps = df_rows(repo.list_deposits())
    contributed = float(deps["amount_usd"].sum()) if not deps.empty else config.capital.total_capital_usd
    eq = df_rows(repo.query("SELECT ts, equity FROM equity_history WHERE mode=? ORDER BY id", (mode,)))
    equity = float(eq["equity"].iloc[-1]) if not eq.empty else contributed
    c1, c2, c3 = st.columns(3)
    c1.metric("Contributed capital", f"${contributed:,.2f}")
    c2.metric("Equity" + (" (virtual)" if mode == "paper" else ""), f"${equity:,.2f}")
    c3.metric("Trading profit (deposits excluded)", f"${equity - contributed:+,.2f}")
    state = repo.get_kv("capital:allocations") or {}
    st.subheader("Current allocation")
    st.json(state)
    st.subheader("Deposits (never counted as income)")
    st.dataframe(deps, use_container_width=True)
    if not eq.empty:
        from trinity.core.capital_growth import CapitalGrowthManager

        pts = [(datetime.fromisoformat(t), float(v)) for t, v in zip(eq["ts"], eq["equity"], strict=True)]
        months = CapitalGrowthManager.monthly_returns(_GrowthView(repo, config), pts)
        st.subheader("Monthly returns (Modified Dietz, deposits excluded)")
        st.dataframe(pd.DataFrame(months), use_container_width=True)
    st.subheader("Reallocation proposals (approval only via scripts/capital.py)")
    st.dataframe(df_rows(repo.query("SELECT * FROM reallocation_proposals ORDER BY id DESC")), use_container_width=True)
    st.subheader("Allocation history")
    st.dataframe(df_rows(repo.query("SELECT * FROM capital_allocations ORDER BY id DESC")), use_container_width=True)


class _GrowthView:
    """Read-only adapter so the dashboard can reuse the Modified Dietz calculation."""

    def __init__(self, repo: Repository, config) -> None:  # noqa: ANN001
        self.repo, self.config = repo, config


def strategies_page(repo: Repository, mode: str) -> None:
    from trinity.ai.performance_analyzer import StrategyAnalytics

    st.header("Strategy statistics" + (" (VIRTUAL)" if mode == "paper" else ""))
    sa = StrategyAnalytics(repo)
    st.dataframe(pd.DataFrame(sa.per_sub_strategy(Mode(mode))).T, use_container_width=True)
    st.subheader("Real vs virtual (never mixed)")
    for sid in ("crypto_momentum", "stock_trend"):
        rv = sa.real_vs_virtual(sid)
        st.write(sid, pd.DataFrame({"paper (virtual)": rv["paper_virtual"], "live (real)": rv["live_real"]}),
                 rv["flags"])
    st.subheader("Degradation flags")
    st.json(repo.get_kv(f"analytics:{mode}:degraded") or {})
    st.subheader("Strategy PnL correlation")
    st.json(repo.get_kv(f"analytics:{mode}:strategy_correlation") or {})
    st.subheader("Exchange API health")
    st.dataframe(df_rows(repo.query("SELECT * FROM api_health ORDER BY id DESC LIMIT 200")), use_container_width=True)
    st.subheader("Funding payments (leveraged positions only)")
    st.dataframe(df_rows(repo.query("SELECT * FROM funding_payments ORDER BY id DESC LIMIT 200")),
                 use_container_width=True)


def system_page(repo: Repository) -> None:
    st.header("System")
    st.subheader("Exchange / bot connection status")
    st.dataframe(df_rows(repo.list_bot_health()), use_container_width=True)
    since = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    st.subheader("System events (48h)")
    st.dataframe(df_rows(repo.query("SELECT ts, level, component, event_type, message FROM system_events "
                                    "WHERE ts>=? ORDER BY id DESC LIMIT 500", (since,))), use_container_width=True)


if __name__ == "__main__":
    main()
