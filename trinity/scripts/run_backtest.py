"""Backtest / walk-forward runner.

    # historical data from CSV (columns: ts,open,high,low,close,volume)
    python trinity/scripts/run_backtest.py --strategy grid --csv data/csv/BTC_USDT_15m.csv --symbol BTC/USDT
    python trinity/scripts/run_backtest.py --strategy trend --csv data/csv/SPY.csv --symbol SPY --walk-forward
    # download public OHLCV via CCXT first (needs internet access to the exchange)
    python trinity/scripts/run_backtest.py --download binance --symbol BTC/USDT --timeframe 15m --bars 5000
    # synthetic data (pipeline self-test only, results are meaningless for real markets)
    python trinity/scripts/run_backtest.py --strategy grid --synthetic
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd  # noqa: E402

from trinity.backtesting.engine import BacktestConfig, BacktestEngine  # noqa: E402
from trinity.backtesting.strategies import GridBacktest, TrendBacktest  # noqa: E402
from trinity.backtesting.walk_forward import (  # noqa: E402
    chronological_split,
    optimize_holdout,
    summarize_folds,
    walk_forward,
)
from trinity.config.settings import PROJECT_DIR, load_app_config  # noqa: E402


def load_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.set_index("ts").sort_index()
    return df[~df.index.duplicated(keep="last")][["open", "high", "low", "close", "volume"]].astype(float)


def synthetic(bars: int, timeframe: str, seed: int = 3, price: float = 100.0, volume: float = 5000,
              drift: float = 0.0) -> pd.DataFrame:
    from trinity.core.clock import SimClock
    from trinity.core.models import AssetClass
    from trinity.exchanges.simulated_market import SimulatedMarket, SymbolSpec

    clock = SimClock(datetime(2026, 1, 1, tzinfo=UTC))
    m = SimulatedMarket("synthetic", AssetClass.CRYPTO,
                        [SymbolSpec("X", price, timeframe, annual_vol=0.5 if timeframe != "1d" else 0.18,
                                    annual_drift=drift, history_bars=bars, bar_volume=volume)],
                        clock, seed=seed)
    candles = asyncio.run(m.fetch_ohlcv("X", timeframe, bars))
    return pd.DataFrame({"open": [c.open for c in candles], "high": [c.high for c in candles],
                         "low": [c.low for c in candles], "close": [c.close for c in candles],
                         "volume": [c.volume for c in candles]}, index=pd.DatetimeIndex([c.ts for c in candles]))


async def download(exchange: str, symbol: str, timeframe: str, bars: int, out: Path) -> Path:
    from trinity.exchanges.crypto_exchange import CCXTExchange

    ex = CCXTExchange(exchange)
    try:
        candles = await ex.fetch_ohlcv(symbol, timeframe, bars)
    finally:
        await ex.close()
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "open", "high", "low", "close", "volume"])
        for c in candles:
            w.writerow([c.ts.isoformat(), c.open, c.high, c.low, c.close, c.volume])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", choices=["grid", "trend"], default="grid")
    ap.add_argument("--csv", type=Path)
    ap.add_argument("--symbol", default="BTC/USDT")
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--walk-forward", action="store_true")
    ap.add_argument("--download")
    ap.add_argument("--timeframe", default="15m")
    ap.add_argument("--bars", type=int, default=3000)
    args = ap.parse_args()
    cfg = load_app_config()
    if args.download:
        out = PROJECT_DIR / "data" / "csv" / f"{args.symbol.replace('/', '_')}_{args.timeframe}.csv"
        print("saved", asyncio.run(download(args.download, args.symbol, args.timeframe, args.bars, out)))
        return 0
    if args.synthetic:
        df = (synthetic(args.bars, "15m") if args.strategy == "grid"
              else synthetic(args.bars, "1d", price=400.0, volume=5e6, drift=0.08))
        print("!!! SYNTHETIC DATA: pipeline self-test only, NOT evidence of real performance !!!")
    elif args.csv:
        df = load_csv(args.csv)
    else:
        ap.error("--csv or --synthetic required")
    inst = cfg.instruments[args.symbol]
    market = inst.to_market_info(args.symbol)
    alloc = cfg.allocation("crypto_grid" if args.strategy == "grid" else "stock_trend")
    bt = BacktestConfig(initial_cash=alloc, maker_fee=inst.maker_fee, taker_fee=inst.taker_fee,
                        qty_step=market.qty_step, min_notional=market.min_notional,
                        periods_per_year=365 * 96 if args.strategy == "grid" else 252)
    if args.strategy == "grid":
        base = cfg.strategies.crypto_grid

        def factory(params: dict):  # noqa: ANN202
            c = base.model_copy(deep=True)
            for k, v in params.items():
                setattr(c.grid, k, v)
            return GridBacktest(c, market, alloc, cfg.risk.max_risk_per_trade_pct)

        space = {"atr_spacing_multiplier": [0.4, 0.6, 0.9], "levels": [2, 3, 4]}
        warmup = base.regime.trend_period + base.regime.slope_lookback + 5
    else:
        base_t = cfg.strategies.stock_trend

        def factory(params: dict):  # noqa: ANN202
            c = base_t.model_copy(update=params)
            return TrendBacktest(c, market, alloc, cfg.risk.max_risk_per_trade_pct, cfg.risk.max_order_notional_usd)

        space = {"stop_atr_multiplier": [1.5, 2.0, 2.5], "take_profit_r": [2.0, 3.0]}
        warmup = base_t.ema_slow + 15
    if args.walk_forward:
        n = len(df) - warmup
        folds = walk_forward(df, factory, space, bt, warmup=warmup, train_bars=int(n * 0.4), test_bars=int(n * 0.15))
        for f in folds:
            print(f"fold train={f.train} test={f.test} params={f.params} "
                  f"OOS return={f.test_metrics.net_return:.2%} trades={f.test_metrics.trades} "
                  f"DD={f.test_metrics.max_drawdown:.2%} bench={f.test_metrics.benchmark_return:.2%}")
        print(json.dumps(summarize_folds(folds), indent=2, default=str))
    else:
        split = chronological_split(len(df), warmup=warmup)
        res = optimize_holdout(df, factory, space, bt, split)
        print("selected on train/validation:", res.best_params)
        for name, m in (("train", res.train), ("validation", res.validation), ("TEST (out-of-sample)", res.test)):
            print(name, json.dumps(m.as_dict(), default=lambda x: round(x, 4) if isinstance(x, float) else x))
        full = BacktestEngine(bt).run(df, factory(res.best_params), warmup)
        print("note: full-period run shown for reference only (includes optimization data):",
              f"{full.metrics.net_return:.2%}")
    print("Backtests are not a guarantee of future returns.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
