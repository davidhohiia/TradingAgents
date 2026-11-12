"""Application assembly (dependency wiring) for paper and live modes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trinity.ai.anomaly_detector import AnomalyDetector
from trinity.bots.arbitrage.bot import ArbitrageBot
from trinity.bots.arbitrage.scanner import VenueLimits
from trinity.bots.base import BaseBot
from trinity.bots.crypto_grid.bot import CryptoGridBot
from trinity.bots.stock_trend.bot import StockTrendBot
from trinity.config.settings import AppConfig, Settings, secret_value
from trinity.core.capital_allocator import CapitalAllocator
from trinity.core.clock import Clock, RealClock
from trinity.core.execution_engine import ExecutionEngine, ReconcileReport
from trinity.core.log import get_logger
from trinity.core.market_calendar import UsEquityCalendar
from trinity.core.models import AssetClass, Mode
from trinity.core.order_gateway import OrderGateway
from trinity.core.portfolio_manager import PortfolioManager
from trinity.core.position_manager import PositionManager
from trinity.database.repository import Repository
from trinity.exchanges.base_exchange import (
    BaseExchange,
    MarketDataSource,
    NotConfigured,
    ReadOnlyMarketData,
)
from trinity.exchanges.crypto_exchange import CCXTExchange
from trinity.exchanges.paper_exchange import PaperExchange
from trinity.exchanges.stock_broker import AlpacaBroker, AlpacaDataProvider, CsvStockData
from trinity.notifications.notifier import LogNotifier, MultiNotifier, Notifier
from trinity.risk.circuit_breaker import CircuitBreakerRegistry
from trinity.risk.risk_manager import RiskManager

log = get_logger(__name__)


@dataclass
class App:
    settings: Settings
    config: AppConfig
    mode: Mode
    repo: Repository
    clock: Clock
    engine: ExecutionEngine
    pm: PortfolioManager
    notifier: MultiNotifier
    venues: dict[str, BaseExchange]
    closables: list[Any] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    async def close(self) -> None:
        for c in self.closables:
            try:
                await c.close()
            except Exception:  # noqa: BLE001
                pass


def _paper_venue(name: str, data: MarketDataSource, config: AppConfig, symbols: list[str], repo: Repository,
                 clock: Clock, mode: Mode, session_check: Any = None) -> PaperExchange:
    markets = {s: config.instruments[s].to_market_info(s) for s in symbols}
    initial = config.paper.initial_balances.get(f"paper_{name}", {})
    sim = config.paper.simulation
    key = f"paper_exchange:{mode.value}:{name}"
    venue = PaperExchange(name, data, markets, initial, clock=clock, taker_slippage_bps=sim.taker_slippage_bps,
                          limit_fill_ratio=sim.limit_fill_ratio, seed=sim.seed, session_check=session_check,
                          on_change=lambda st: repo.set_kv(key, st))
    state = repo.get_kv(key)
    if state:
        venue.load_state(state)
    return venue


def build_app(
    settings: Settings,
    config: AppConfig,
    repo: Repository,
    mode: Mode,
    *,
    clock: Clock | None = None,
    crypto_data: MarketDataSource | None = None,
    stock_data: MarketDataSource | None = None,
    arb_sources: dict[str, MarketDataSource] | None = None,
    extra_notifiers: list[Notifier] | None = None,
) -> App:
    clock = clock or RealClock()
    notes: list[str] = []
    closables: list[Any] = []
    calendar = UsEquityCalendar(config.session)
    strategies = config.strategies

    # ---------------------------------------------------------------- market data
    if crypto_data is None and strategies.crypto_grid.enabled:
        fallback = {s: config.instruments[s].to_market_info(s) for s in strategies.crypto_grid.symbols}
        crypto_data = CCXTExchange(
            settings.crypto_exchange_id,
            api_key=secret_value(settings.crypto_api_key) if mode == Mode.LIVE else None,
            api_secret=secret_value(settings.crypto_api_secret) if mode == Mode.LIVE else None,
            api_password=secret_value(settings.crypto_api_password) if mode == Mode.LIVE else None,
            instruments_fallback=fallback,
        )
        closables.append(crypto_data)
    if stock_data is None and strategies.stock_trend.enabled:
        if settings.stock_data_provider == "alpaca" and settings.has_alpaca_credentials():
            stock_data = AlpacaDataProvider(secret_value(settings.alpaca_api_key_id),
                                            secret_value(settings.alpaca_api_secret_key), settings.alpaca_data_feed)
            closables.append(stock_data)
        elif settings.stock_data_provider == "csv":
            stock_data = CsvStockData(settings.stock_csv_dir)
            notes.append("stock data from CSV files: quotes are derived from last close (not live)")
        else:
            notes.append("stock_trend: no market data provider configured (set ALPACA keys) -> bot not started")

    # ---------------------------------------------------------------- venues
    venues: dict[str, BaseExchange] = {}
    if mode == Mode.PAPER:
        if crypto_data is not None:
            venues["crypto"] = _paper_venue("crypto", crypto_data, config, strategies.crypto_grid.symbols, repo,
                                            clock, mode)
        if stock_data is not None:
            venues["stocks"] = _paper_venue("stocks", stock_data, config, strategies.stock_trend.symbols, repo,
                                            clock, mode, session_check=calendar.is_open)
    else:
        if strategies.crypto_grid.enabled and isinstance(crypto_data, CCXTExchange):
            crypto_data.name = "crypto"
            venues["crypto"] = crypto_data
        if strategies.stock_trend.enabled:
            try:
                fallback = {s: config.instruments[s].to_market_info(s) for s in strategies.stock_trend.symbols}
                broker = AlpacaBroker(secret_value(settings.alpaca_api_key_id), secret_value(settings.alpaca_api_secret_key),
                                      paper_account=settings.alpaca_paper_account, instruments_fallback=fallback,
                                      data=stock_data)
                broker.name = "stocks"
                venues["stocks"] = broker
                closables.append(broker)
            except NotConfigured as exc:
                notes.append(f"stock broker not configured: {exc}")

    # ---------------------------------------------------------------- core services
    breakers = CircuitBreakerRegistry(config.risk.api_failure_threshold, config.risk.api_cooldown_seconds, clock)
    risk = RiskManager(config, repo, mode, breakers, clock)
    allocator = CapitalAllocator(config)
    positions = PositionManager(repo, config, mode)
    engine = ExecutionEngine(config=config, repo=repo, mode=mode, risk=risk, allocator=allocator,
                             positions=positions, venues=venues, breakers=breakers, clock=clock,
                             usdt_usd_rate=config.currency.usdt_usd_rate)
    notifier = MultiNotifier(LogNotifier(), *(extra_notifiers or []))
    anomaly = AnomalyDetector()

    bots: list[BaseBot] = []
    common = {"repo": repo, "mode": mode, "clock": clock, "notifier": notifier}
    if strategies.crypto_grid.enabled and "crypto" in venues:
        bots.append(CryptoGridBot(strategies.crypto_grid, anomaly=anomaly,
                                  gateway=OrderGateway(engine, strategies.crypto_grid.strategy_id, "crypto"),
                                  market=ReadOnlyMarketData(venues["crypto"]), **common))
    if strategies.stock_trend.enabled and "stocks" in venues:
        bots.append(StockTrendBot(strategies.stock_trend, config, calendar, anomaly=None,
                                  gateway=OrderGateway(engine, strategies.stock_trend.strategy_id, "stocks"),
                                  market=ReadOnlyMarketData(venues["stocks"]), **common))
    if strategies.arbitrage.enabled:
        if arb_sources is None:
            arb_sources = {}
            for ex_id in settings.arb_exchanges:
                try:
                    src = CCXTExchange(ex_id)  # public market data only, never keys
                    arb_sources[ex_id] = src
                    closables.append(src)
                except NotConfigured as exc:
                    notes.append(f"arbitrage: {exc}")
        limits = {(v, s): VenueLimits(config.instruments[s].min_qty, config.instruments[s].min_notional)
                  for v in arb_sources for s in strategies.arbitrage.symbols if s in config.instruments}
        if len(arb_sources) >= 2:
            bots.append(ArbitrageBot(strategies.arbitrage, arb_sources, limits=limits, anomaly=anomaly, **common))
        else:
            notes.append("arbitrage: fewer than 2 exchanges configured -> scanner not started")

    pm = PortfolioManager(config=config, repo=repo, engine=engine, mode=mode, bots=bots, clock=clock,
                          notifier=notifier)
    return App(settings, config, mode, repo, clock, engine, pm, notifier, venues, closables, notes)


async def connect_venues(app: App) -> dict[str, str]:
    """Load markets for real-data adapters. Failures are reported, never faked."""
    results: dict[str, str] = {}
    seen: set[int] = set()
    targets: list[tuple[str, Any]] = []
    for name, v in app.venues.items():
        inner = getattr(v, "data", v)
        targets.append((name, inner))
    for bot in app.pm.bots.values():
        if isinstance(bot, ArbitrageBot):
            targets.extend(bot.sources.items())
    for name, obj in targets:
        if id(obj) in seen or not hasattr(obj, "connect"):
            continue
        seen.add(id(obj))
        try:
            await obj.connect()
            results[name] = "connected"
        except Exception as exc:  # noqa: BLE001
            results[name] = f"FAILED: {type(exc).__name__}: {str(exc)[:200]}"
            if name in app.venues:
                app.engine.venue_connected[name] = False
            app.repo.add_system_event("ERROR", "venue", "connect_failed", f"{name}: {exc}", ts=app.clock.now())
    return results


async def startup_reconcile(app: App) -> list[ReconcileReport]:
    """Reconcile every venue before trading. Failure => recovery lock (live trading blocked)."""
    reports = []
    for name in app.venues:
        rep = await app.engine.reconcile(name)
        reports.append(rep)
    recovery = app.repo.get_kv("recovery_required")
    failed = [r for r in reports if not r.ok]
    if failed:
        msg = "; ".join(f"{r.venue}: {', '.join(r.issues)}" for r in failed)
        app.engine.risk.set_recovery_lock(f"startup reconciliation failed: {msg}")
        app.repo.add_risk_event(app.mode, "CRITICAL", "RECONCILIATION_FAILED", msg, ts=app.clock.now())
        if app.mode == Mode.LIVE:
            app.engine.risk.disable_live("startup reconciliation failed")
        await app.notifier.notify("risk_limit", f"Reconciliation failed, trading locked: {msg}", "CRITICAL")
    else:
        app.engine.risk.clear_recovery_lock()
        if recovery:
            app.repo.delete_kv("recovery_required")
    return reports


def asset_class_of(symbol: str, config: AppConfig) -> AssetClass:
    return config.instruments[symbol].asset_class
