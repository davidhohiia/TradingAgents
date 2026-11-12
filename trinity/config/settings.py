"""Configuration loading and validation.

Secrets come ONLY from environment variables / the .env file (never YAML, never the DB).
Strategy, capital and risk parameters come from config/strategies.yaml and
config/instruments.yaml and are validated with pydantic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from trinity.core.log import register_secret
from trinity.core.models import AssetClass, MarketInfo

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_DIR / "config"

LIVE_ACKNOWLEDGEMENT_PHRASE = "I UNDERSTAND THAT LIVE TRADING CAN LOSE REAL MONEY"


# --------------------------------------------------------------------------- env settings
class Settings(BaseSettings):
    """Environment settings (.env). Secrets are SecretStr so they never print."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_DIR / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    paper_trading: bool = True
    live_trading: bool = False
    live_trading_acknowledgement: str = ""

    database_path: Path = PROJECT_DIR / "data" / "trinity.db"
    backup_dir: Path = PROJECT_DIR / "data" / "backups"
    log_dir: Path = PROJECT_DIR / "logs"
    log_level: str = "INFO"
    lock_file: Path = PROJECT_DIR / "data" / "trinity.lock"

    telegram_bot_token: SecretStr | None = None
    telegram_allowed_user_ids: str = ""

    crypto_exchange_id: str = "binance"
    crypto_api_key: SecretStr | None = None
    crypto_api_secret: SecretStr | None = None
    crypto_api_password: SecretStr | None = None

    arb_exchange_ids: str = "binance,kraken,okx"

    stock_broker: Literal["alpaca", "none"] = "alpaca"
    alpaca_api_key_id: SecretStr | None = None
    alpaca_api_secret_key: SecretStr | None = None
    alpaca_paper_account: bool = True
    alpaca_data_feed: str = "iex"
    stock_data_provider: Literal["alpaca", "csv", "none"] = "alpaca"
    stock_csv_dir: Path = PROJECT_DIR / "data" / "csv"

    account_currency: Literal["USD", "EUR"] = "USD"
    eur_usd_rate: float | None = None

    dashboard_port: int = 8501

    @property
    def allowed_telegram_ids(self) -> set[int]:
        ids: set[int] = set()
        for part in self.telegram_allowed_user_ids.replace(";", ",").split(","):
            part = part.strip()
            if part.lstrip("-").isdigit():
                ids.add(int(part))
        return ids

    @property
    def arb_exchanges(self) -> list[str]:
        return [x.strip().lower() for x in self.arb_exchange_ids.split(",") if x.strip()]

    def has_crypto_credentials(self) -> bool:
        return bool(_secret(self.crypto_api_key) and _secret(self.crypto_api_secret))

    def has_alpaca_credentials(self) -> bool:
        return bool(_secret(self.alpaca_api_key_id) and _secret(self.alpaca_api_secret_key))

    def register_secrets(self) -> None:
        for value in (
            self.telegram_bot_token,
            self.crypto_api_key,
            self.crypto_api_secret,
            self.crypto_api_password,
            self.alpaca_api_key_id,
            self.alpaca_api_secret_key,
        ):
            register_secret(_secret(value))


def _secret(value: SecretStr | None) -> str:
    return value.get_secret_value() if value is not None else ""


def secret_value(value: SecretStr | None) -> str:
    return _secret(value)


# --------------------------------------------------------------------------- YAML models
class CapitalConfig(BaseModel):
    total_capital_usd: float = Field(gt=0)
    allocations: dict[str, float]
    reserve_usd: float = Field(ge=0)
    allow_profit_reinvestment: bool = False

    @model_validator(mode="after")
    def _check_sum(self) -> CapitalConfig:
        if any(v < 0 for v in self.allocations.values()):
            raise ValueError("allocations must be >= 0")
        total = sum(self.allocations.values()) + self.reserve_usd
        if total > self.total_capital_usd + 1e-9:
            raise ValueError(
                f"allocations + reserve ({total}) exceed total capital ({self.total_capital_usd})"
            )
        if self.allow_profit_reinvestment:
            raise ValueError("automatic profit reinvestment is not allowed in this version")
        return self


class RiskConfig(BaseModel):
    leverage: float = 0
    allow_margin: bool = False
    allow_short: bool = False
    max_risk_per_trade_pct: float = Field(0.01, gt=0, le=0.01)
    min_risk_per_trade_pct: float = Field(0.005, gt=0, le=0.01)
    max_daily_loss_pct: float = Field(0.02, gt=0, le=0.10)
    max_drawdown_pct: float = Field(0.08, gt=0, le=0.25)
    drawdown_warning_pct: float = Field(0.05, gt=0)
    max_total_exposure_pct: float = Field(0.60, gt=0, le=1.0)
    max_order_notional_usd: float = Field(75, gt=0)
    min_order_notional_usd: float = Field(5, ge=0)
    max_position_pct_of_strategy: float = Field(0.6, gt=0, le=1.0)
    max_open_positions: int = Field(6, ge=1)
    max_open_orders: int = Field(30, ge=1)
    max_correlated_exposure_pct: float = Field(0.30, gt=0, le=1.0)
    max_price_age_seconds: dict[str, float] = Field(
        default_factory=lambda: {"crypto": 30, "stock": 180}
    )
    api_failure_threshold: int = Field(5, ge=1)
    api_cooldown_seconds: float = Field(120, gt=0)
    max_orders_per_minute: int = Field(20, ge=1)

    @model_validator(mode="after")
    def _check(self) -> RiskConfig:
        if self.leverage != 0:
            raise ValueError("leverage must be 0 (leverage is forbidden)")
        if self.allow_margin:
            raise ValueError("margin trading is forbidden")
        if self.allow_short:
            raise ValueError("short selling is forbidden")
        if self.min_risk_per_trade_pct > self.max_risk_per_trade_pct:
            raise ValueError("min_risk_per_trade_pct > max_risk_per_trade_pct")
        if self.drawdown_warning_pct >= self.max_drawdown_pct:
            raise ValueError("drawdown_warning_pct must be below max_drawdown_pct")
        return self

    def price_age_limit(self, asset_class: AssetClass) -> float:
        return float(self.max_price_age_seconds.get(asset_class.value, 30))


class PaperSimulationConfig(BaseModel):
    taker_slippage_bps: float = 5
    limit_fill_ratio: float = Field(0.5, gt=0, le=1)
    stock_spread_bps: float = 2
    seed: int = 42


class PaperConfig(BaseModel):
    min_days: int = 30
    min_closed_trades: int = 100
    require_paper_validation: bool = True
    initial_balances: dict[str, dict[str, float]] = Field(default_factory=dict)
    simulation: PaperSimulationConfig = PaperSimulationConfig()


class GridParams(BaseModel):
    levels: int = Field(3, ge=1, le=20)
    level_notional_usd: float = Field(20, gt=0)
    atr_period: int = 14
    atr_spacing_multiplier: float = 0.6
    min_spacing_pct: float = Field(0.006, gt=0)
    max_spacing_pct: float = Field(0.03, gt=0)
    max_inventory_usd_per_symbol: float = Field(60, gt=0)
    stop_atr_multiplier: float = 2.0
    stop_cooldown_minutes: float = 240
    rebuild_drift_spacings: float = 1.5
    rebuild_atr_change_pct: float = 0.35
    max_order_age_minutes: float = 240
    min_net_profit_usd: float = 0.05
    min_net_profit_pct: float = 0.001
    adverse_spread_buffer: float = 0.5

    @model_validator(mode="after")
    def _check(self) -> GridParams:
        if self.min_spacing_pct >= self.max_spacing_pct:
            raise ValueError("min_spacing_pct must be < max_spacing_pct")
        return self


class RegimeParams(BaseModel):
    trend_period: int = 50
    adx_period: int = 14
    adx_trend_threshold: float = 25
    strong_bear_slope_pct: float = -0.02
    slope_lookback: int = 20
    high_vol_atr_pct: float = 0.04
    allowed_regimes: list[str] = Field(default_factory=lambda: ["ranging", "bull"])


class GridStrategyConfig(BaseModel):
    enabled: bool = True
    strategy_id: str = "crypto_grid"
    venue: str = "crypto"
    symbols: list[str]
    timeframe: str = "15m"
    history_bars: int = 300
    loop_interval_seconds: float = 30
    grid: GridParams = GridParams()
    regime: RegimeParams = RegimeParams()


class StockTrendConfig(BaseModel):
    enabled: bool = True
    strategy_id: str = "stock_trend"
    venue: str = "stocks"
    symbols: list[str]
    timeframe: str = "1Day"
    history_bars: int = 260
    loop_interval_seconds: float = 60
    ema_fast: int = 20
    ema_slow: int = 50
    rsi_period: int = 14
    rsi_min: float = 45
    rsi_max: float = 70
    atr_period: int = 14
    volume_lookback: int = 20
    min_up_down_volume_ratio: float = 1.0
    min_avg_dollar_volume: float = 50_000_000
    stop_atr_multiplier: float = 2.0
    take_profit_r: float = 2.5
    trailing_atr_multiplier: float = 3.0
    trailing_activation_r: float = 1.0
    min_reward_risk: float = 1.5
    max_positions: int = 2
    entry_limit_offset_bps: float = 10
    max_spread_bps: float = 20
    allow_fractional: bool = True

    @model_validator(mode="after")
    def _check(self) -> StockTrendConfig:
        if self.ema_fast >= self.ema_slow:
            raise ValueError("ema_fast must be < ema_slow")
        if not 0 < self.rsi_min < self.rsi_max < 100:
            raise ValueError("invalid RSI range")
        return self


class ArbitrageConfig(BaseModel):
    enabled: bool = True
    strategy_id: str = "arbitrage"
    mode: Literal["scan_only"] = "scan_only"
    exchanges: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=lambda: ["BTC/USDT", "ETH/USDT"])
    loop_interval_seconds: float = 10
    trade_size_usd: float = Field(100, gt=0)
    orderbook_depth: int = 20
    min_net_profit_usd: float = 0.10
    min_net_profit_bps: float = 5
    slippage_bps: float = 5
    max_quote_age_ms: float = 3000
    max_clock_skew_ms: float = 2000
    assumed_latency_ms: float = 500
    max_execution_risk_usd: float = 0.5
    volatility_per_second_bps: float = 2.0
    transfer_amortization_trades: int = Field(10, ge=1)
    transfer_fees: dict[str, float] = Field(default_factory=dict)
    taker_fees: dict[str, float] = Field(default_factory=lambda: {"default": 0.001})
    virtual_capital_usd: float = 200

    def taker_fee(self, exchange: str) -> float:
        return float(self.taker_fees.get(exchange, self.taker_fees.get("default", 0.001)))


class StrategiesConfig(BaseModel):
    crypto_grid: GridStrategyConfig
    stock_trend: StockTrendConfig
    arbitrage: ArbitrageConfig


class InstrumentConfig(BaseModel):
    asset_class: AssetClass
    base: str
    quote: str
    correlation_group: str = ""
    qty_step: float = Field(gt=0)
    price_step: float = Field(gt=0)
    min_qty: float = Field(ge=0)
    min_notional: float = Field(ge=0)
    maker_fee: float = Field(ge=0, le=0.01)
    taker_fee: float = Field(ge=0, le=0.01)
    sell_regulatory_fee_pct: float = 0.0

    def to_market_info(self, symbol: str, fractional: bool = True) -> MarketInfo:
        return MarketInfo(
            symbol=symbol,
            base=self.base,
            quote=self.quote,
            asset_class=self.asset_class,
            qty_step=self.qty_step if fractional else 1.0,
            price_step=self.price_step,
            min_qty=self.min_qty if fractional else 1.0,
            min_notional=self.min_notional,
            maker_fee=self.maker_fee,
            taker_fee=self.taker_fee,
            fractional=fractional,
        )


class SessionConfig(BaseModel):
    timezone: str = "America/New_York"
    open: str = "09:30"
    close: str = "16:00"
    early_close: str = "13:00"
    holidays: list[str] = Field(default_factory=list)
    early_closes: list[str] = Field(default_factory=list)


class CurrencyConfig(BaseModel):
    usdt_usd_rate: float = Field(1.0, gt=0)


class InstrumentsFile(BaseModel):
    instruments: dict[str, InstrumentConfig]
    us_equity_session: SessionConfig = SessionConfig()
    currency: CurrencyConfig = CurrencyConfig()


class AppConfig(BaseModel):
    capital: CapitalConfig
    risk: RiskConfig
    paper: PaperConfig
    strategies: StrategiesConfig
    instruments: dict[str, InstrumentConfig]
    session: SessionConfig
    currency: CurrencyConfig

    @field_validator("instruments")
    @classmethod
    def _non_empty(cls, v: dict[str, InstrumentConfig]) -> dict[str, InstrumentConfig]:
        if not v:
            raise ValueError("no instruments configured")
        return v

    @model_validator(mode="after")
    def _cross_check(self) -> AppConfig:
        for sym in self.strategies.crypto_grid.symbols + self.strategies.stock_trend.symbols:
            if sym not in self.instruments:
                raise ValueError(f"strategy symbol {sym} missing from instruments.yaml")
        grid = self.strategies.crypto_grid
        budget = self.capital.allocations.get(grid.strategy_id, 0)
        if grid.enabled and budget > 0:
            per_symbol = budget / max(len(grid.symbols), 1)
            if grid.grid.max_inventory_usd_per_symbol > per_symbol + 1e-9:
                raise ValueError("grid max_inventory_usd_per_symbol exceeds per-symbol budget")
        return self

    def correlation_group(self, symbol: str) -> str:
        inst = self.instruments.get(symbol)
        return inst.correlation_group if inst and inst.correlation_group else symbol

    def allocation(self, strategy_id: str) -> float:
        return float(self.capital.allocations.get(strategy_id, 0.0))


def _read_yaml(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping")
    return data


def load_app_config(config_dir: Path | None = None) -> AppConfig:
    config_dir = config_dir or CONFIG_DIR
    strat = _read_yaml(config_dir / "strategies.yaml")
    inst = InstrumentsFile.model_validate(_read_yaml(config_dir / "instruments.yaml"))
    return AppConfig(
        capital=CapitalConfig.model_validate(strat["capital"]),
        risk=RiskConfig.model_validate(strat["risk"]),
        paper=PaperConfig.model_validate(strat.get("paper", {})),
        strategies=StrategiesConfig.model_validate(strat["strategies"]),
        instruments=inst.instruments,
        session=inst.us_equity_session,
        currency=inst.currency,
    )


def load_settings(env_file: Path | None = None) -> Settings:
    if env_file is not None:
        settings = Settings(_env_file=str(env_file))  # type: ignore[call-arg]
    else:
        settings = Settings()
    settings.register_secrets()
    return settings
