"""Shared fixtures. Tests NEVER touch real venues: only PaperExchange + SimulatedMarket / mocks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from trinity.config.settings import AppConfig, load_app_config
from trinity.core.capital_allocator import CapitalAllocator
from trinity.core.clock import SimClock
from trinity.core.execution_engine import ExecutionEngine
from trinity.core.models import AssetClass, Mode
from trinity.core.position_manager import PositionManager
from trinity.database.repository import Repository
from trinity.exchanges.paper_exchange import PaperExchange
from trinity.exchanges.simulated_market import SimulatedMarket, SymbolSpec
from trinity.risk.circuit_breaker import CircuitBreakerRegistry
from trinity.risk.risk_manager import RiskManager

START = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)  # a Monday, US session open (10:00 New York)


@dataclass
class System:
    config: AppConfig
    repo: Repository
    clock: SimClock
    crypto_market: SimulatedMarket
    stock_market: SimulatedMarket
    crypto: PaperExchange
    stocks: PaperExchange
    risk: RiskManager
    allocator: CapitalAllocator
    positions: PositionManager
    engine: ExecutionEngine
    breakers: CircuitBreakerRegistry


@pytest.fixture
def config() -> AppConfig:
    return load_app_config()


def build_system(tmp_path: Path, config: AppConfig, *, mode: Mode = Mode.PAPER, seed: int = 7,
                 db_path: Path | None = None, crypto_balance: float = 150.0, stock_balance: float = 150.0,
                 clock: SimClock | None = None) -> System:
    clock = clock or SimClock(START)
    repo = Repository(db_path or tmp_path / "test.db")
    repo.migrate()
    crypto_market = SimulatedMarket(
        "sim-crypto", AssetClass.CRYPTO,
        [SymbolSpec("BTC/USDT", 60000, "15m", annual_vol=0.5, level_qty_usd=50_000),
         SymbolSpec("ETH/USDT", 3000, "15m", annual_vol=0.6, level_qty_usd=50_000)],
        clock, seed=seed,
    )
    stock_market = SimulatedMarket(
        "sim-stocks", AssetClass.STOCK,
        [SymbolSpec("SPY", 500, "1Day", annual_vol=0.18, annual_drift=0.25, bar_volume=80_000_000, history_bars=260),
         SymbolSpec("QQQ", 430, "1Day", annual_vol=0.22, annual_drift=0.25, bar_volume=40_000_000, history_bars=260),
         SymbolSpec("VTI", 250, "1Day", annual_vol=0.17, annual_drift=0.25, bar_volume=4_000_000, history_bars=260)],
        clock, seed=seed + 1,
    )
    markets_c = {s: config.instruments[s].to_market_info(s) for s in ("BTC/USDT", "ETH/USDT")}
    markets_s = {s: config.instruments[s].to_market_info(s) for s in ("SPY", "QQQ", "VTI")}
    crypto = PaperExchange("crypto", crypto_market, markets_c, {"USDT": crypto_balance}, clock=clock,
                           book_cache_seconds=0)
    stocks = PaperExchange("stocks", stock_market, markets_s, {"USD": stock_balance}, clock=clock,
                           book_cache_seconds=0)
    breakers = CircuitBreakerRegistry(config.risk.api_failure_threshold, config.risk.api_cooldown_seconds, clock)
    risk = RiskManager(config, repo, mode, breakers, clock)
    allocator = CapitalAllocator(config)
    positions = PositionManager(repo, config, mode)
    engine = ExecutionEngine(config=config, repo=repo, mode=mode, risk=risk, allocator=allocator,
                             positions=positions, venues={"crypto": crypto, "stocks": stocks},
                             breakers=breakers, clock=clock)
    return System(config, repo, clock, crypto_market, stock_market, crypto, stocks, risk, allocator,
                  positions, engine, breakers)


@pytest.fixture
def system(tmp_path: Path, config: AppConfig) -> System:
    return build_system(tmp_path, config)


# --------------------------------------------------------------------------- test result recording
# The live-trading gate requires a recent, fully passing, full-suite run on the exact same code.
_results = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}


def pytest_runtest_logreport(report):  # noqa: ANN001, ANN201
    if report.when == "call":
        if report.passed:
            _results["passed"] += 1
        elif report.failed:
            _results["failed"] += 1
        elif report.skipped:
            _results["skipped"] += 1
    elif report.failed:
        _results["errors"] += 1


def pytest_sessionfinish(session, exitstatus):  # noqa: ANN001, ANN201
    import json

    from trinity.core.live_guard import TEST_RESULTS_PATH, source_hash

    opt = session.config.option
    if getattr(opt, "collectonly", False):
        return  # nothing was executed: never overwrite real results
    args = [a for a in session.config.args if not a.endswith(("tests", "tests/"))]
    full = not (opt.keyword or opt.markexpr or args or getattr(opt, "lf", False) or opt.maxfail)
    payload = {**_results, "exit_status": int(exitstatus), "full_suite": bool(full),
               "collected": session.testscollected, "ts": datetime.now(UTC).isoformat(),
               "code_hash": source_hash()}
    try:
        TEST_RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        TEST_RESULTS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        pass
