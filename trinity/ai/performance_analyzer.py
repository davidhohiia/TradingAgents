"""Performance Analyzer: strategy statistics, live/paper vs backtest comparison,
degradation detection and parameter RECOMMENDATIONS.

Recommendations are advisory only. They are stored for human review and are never
applied automatically to live (or paper) trading parameters.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from trinity.core.models import Mode
from trinity.database.repository import Repository


@dataclass
class StrategyPerformance:
    strategy_id: str
    mode: str
    trades: int
    net_pnl: float
    fees: float
    win_rate: float | None
    profit_factor: float | None
    avg_trade: float | None
    sample_note: str = ""


@dataclass
class Recommendation:
    strategy_id: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)
    auto_apply: bool = False  # always False: humans decide


class PerformanceAnalyzer:
    MIN_SAMPLE = 30

    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    def strategy_performance(self, strategy_id: str, mode: Mode) -> StrategyPerformance:
        s = self.repo.trade_stats(mode, strategy_id)
        n = int(s["n"] or 0)
        pf = (s["gross_win"] / s["gross_loss"]) if s["gross_loss"] else (math.inf if s["gross_win"] else None)
        perf = StrategyPerformance(
            strategy_id, mode.value, n, float(s["pnl"]), float(s["fees"]),
            (s["wins"] / n) if n else None, pf, (s["pnl"] / n) if n else None,
        )
        if n < self.MIN_SAMPLE:
            perf.sample_note = f"only {n} closed trades: statistics are not reliable (< {self.MIN_SAMPLE})"
        return perf

    def compare_with_backtest(self, perf: StrategyPerformance, backtest: dict[str, float]) -> list[str]:
        notes: list[str] = []
        if perf.trades < self.MIN_SAMPLE:
            return [perf.sample_note]
        bt_wr = backtest.get("win_rate")
        if bt_wr is not None and perf.win_rate is not None and perf.win_rate < bt_wr - 0.15:
            notes.append(f"win rate {perf.win_rate:.0%} far below backtest {bt_wr:.0%}")
        bt_avg = backtest.get("avg_trade")
        if bt_avg is not None and perf.avg_trade is not None and bt_avg > 0 and perf.avg_trade < 0:
            notes.append(f"average trade negative ({perf.avg_trade:.4f}) while backtest was positive ({bt_avg:.4f})")
        bt_pf = backtest.get("profit_factor")
        if bt_pf and perf.profit_factor is not None and perf.profit_factor < min(1.0, bt_pf * 0.6):
            notes.append(f"profit factor {perf.profit_factor:.2f} degraded vs backtest {bt_pf:.2f}")
        return notes

    def detect_degradation(self, strategy_id: str, mode: Mode, window: int = 30) -> list[str]:
        rows = self.repo.list_trades(mode, limit=window * 2, strategy_id=strategy_id)
        if len(rows) < window * 2:
            return []
        recent = [r["realized_pnl"] for r in rows[:window]]
        older = [r["realized_pnl"] for r in rows[window:]]
        out = []
        if sum(recent) < 0 <= sum(older):
            out.append(f"last {window} trades net {sum(recent):.2f} vs previous {window} net {sum(older):.2f}")
        wr_recent = sum(1 for x in recent if x > 0) / window
        wr_old = sum(1 for x in older if x > 0) / window
        if wr_recent < wr_old - 0.2:
            out.append(f"win rate dropped from {wr_old:.0%} to {wr_recent:.0%}")
        return out

    def recommendations(self, strategy_id: str, mode: Mode) -> list[Recommendation]:
        perf = self.strategy_performance(strategy_id, mode)
        recs: list[Recommendation] = []
        if perf.trades < self.MIN_SAMPLE:
            recs.append(Recommendation(strategy_id, f"Keep collecting data: {perf.sample_note}.",
                                       {"trades": perf.trades}))
            return recs
        if perf.fees and perf.net_pnl and perf.fees > abs(perf.net_pnl):
            recs.append(Recommendation(strategy_id, "Fees exceed net result: consider wider grid spacing / fewer trades "
                                       "(re-validate with walk-forward backtest first).",
                                       {"fees": perf.fees, "net": perf.net_pnl}))
        if perf.profit_factor is not None and perf.profit_factor < 1.0:
            recs.append(Recommendation(strategy_id, "Profit factor < 1: consider pausing the strategy and re-testing "
                                       "on out-of-sample data.", {"profit_factor": perf.profit_factor}))
        for d in self.detect_degradation(strategy_id, mode):
            recs.append(Recommendation(strategy_id, f"Performance degradation: {d}", {}))
        if not recs:
            recs.append(Recommendation(strategy_id, "No change recommended.", {}))
        return recs


# ---------------------------------------------------------------------- TRINITY 3.0 extensions
def _stats_dict(s: dict[str, float]) -> dict[str, float | int | None]:
    n = int(s["n"] or 0)
    pf = (s["gross_win"] / s["gross_loss"]) if s["gross_loss"] else (math.inf if s["gross_win"] else None)
    return {"trades": n, "net_pnl": float(s["pnl"]), "fees": float(s["fees"]),
            "win_rate": (s["wins"] / n) if n else None, "profit_factor": pf,
            "avg_trade": (s["pnl"] / n) if n else None}


class StrategyAnalytics(PerformanceAnalyzer):
    """Per-(sub)strategy statistics, real-vs-virtual control, strategy correlation, degradation."""

    SUBS = {"crypto_momentum": ["momentum", "breakout"], "stock_trend": ["trend", "breakout"]}

    def per_sub_strategy(self, mode: Mode) -> dict[str, dict[str, float | int | None]]:
        out = {}
        for sid, subs in self.SUBS.items():
            out[sid] = _stats_dict(self.repo.trade_stats(mode, sid))
            for sub in subs:
                out[f"{sid}.{sub}"] = _stats_dict(self.repo.trade_stats(mode, sid, sub_strategy=sub))
        return out

    def real_vs_virtual(self, strategy_id: str) -> dict[str, Any]:
        paper = _stats_dict(self.repo.trade_stats(Mode.PAPER, strategy_id))
        live = _stats_dict(self.repo.trade_stats(Mode.LIVE, strategy_id))
        flags = []
        if live["trades"] and live["trades"] >= self.MIN_SAMPLE and paper["trades"] >= self.MIN_SAMPLE:
            if paper["avg_trade"] and live["avg_trade"] is not None and live["avg_trade"] < paper["avg_trade"] * 0.5:
                flags.append("live average trade below 50% of paper: check slippage/fees/latency")
            if paper["win_rate"] and live["win_rate"] is not None and live["win_rate"] < paper["win_rate"] - 0.15:
                flags.append("live win rate >15pp below paper")
        elif live["trades"]:
            flags.append(f"only {live['trades']} live trades: comparison not yet meaningful")
        return {"strategy": strategy_id, "paper_virtual": paper, "live_real": live, "flags": flags}

    def daily_pnl(self, mode: Mode) -> dict[str, pd.Series]:
        rows = self.repo.query("SELECT strategy_id, substr(exit_time,1,10) d, SUM(realized_pnl) p FROM trades "
                               "WHERE mode=? GROUP BY strategy_id, d", (mode.value,))
        series: dict[str, dict[str, float]] = {}
        for r in rows:
            series.setdefault(r["strategy_id"], {})[r["d"]] = float(r["p"])
        return {k: pd.Series(v) for k, v in series.items()}

    def strategy_correlations(self, mode: Mode) -> dict[str, float]:
        from trinity.risk.correlation import strategy_correlation

        return {f"{a}~{b}": v for (a, b), v in strategy_correlation(self.daily_pnl(mode)).items()}

    def degradation_report(self, mode: Mode, window: int = 20) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for sid, subs in self.SUBS.items():
            for key, sub in [(sid, None)] + [(f"{sid}.{s}", s) for s in subs]:
                rows = [r for r in self.repo.list_trades(mode, limit=1000, strategy_id=sid)
                        if sub is None or r["sub_strategy"] == sub][: window * 2]
                if len(rows) < window * 2:
                    continue
                recent = [r["realized_pnl"] for r in rows[:window]]
                older = [r["realized_pnl"] for r in rows[window:]]
                notes = []
                if sum(recent) < 0 <= sum(older):
                    notes.append(f"last {window} trades net {sum(recent):.2f} vs previous {sum(older):.2f}")
                wr_r = sum(1 for x in recent if x > 0) / window
                wr_o = sum(1 for x in older if x > 0) / window
                if wr_r < wr_o - 0.2:
                    notes.append(f"win rate {wr_o:.0%} -> {wr_r:.0%}")
                if notes:
                    out[key] = notes
        return out
