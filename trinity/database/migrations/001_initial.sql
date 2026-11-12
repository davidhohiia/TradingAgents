-- TRINITY initial schema. All timestamps are ISO-8601 UTC strings.
-- API secrets are NEVER stored in this database.

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    venue TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
    currency TEXT NOT NULL DEFAULT 'USD',
    description TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (venue, mode)
);

CREATE TABLE IF NOT EXISTS strategies (
    strategy_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    bot_type TEXT NOT NULL,
    allocation_usd REAL NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS balances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    venue TEXT NOT NULL,
    mode TEXT NOT NULL,
    asset TEXT NOT NULL,
    free REAL NOT NULL,
    used REAL NOT NULL,
    total REAL NOT NULL,
    usd_value REAL,
    ts TEXT NOT NULL,
    UNIQUE (venue, mode, asset)
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL,
    venue TEXT NOT NULL,
    symbol TEXT NOT NULL,
    mode TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    correlation_group TEXT NOT NULL DEFAULT '',
    quantity REAL NOT NULL DEFAULT 0,
    avg_entry_price REAL NOT NULL DEFAULT 0,
    realized_pnl REAL NOT NULL DEFAULT 0,
    fees_paid REAL NOT NULL DEFAULT 0,
    last_price REAL,
    unrealized_pnl REAL NOT NULL DEFAULT 0,
    stop_price REAL,
    take_profit REAL,
    opened_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (strategy_id, venue, symbol, mode)
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT NOT NULL UNIQUE,
    exchange_order_id TEXT,
    strategy_id TEXT NOT NULL,
    venue TEXT NOT NULL,
    symbol TEXT NOT NULL,
    mode TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    order_type TEXT NOT NULL,
    quantity REAL NOT NULL CHECK (quantity > 0),
    price REAL,
    stop_price REAL,
    take_profit REAL,
    reduce_only INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    filled_quantity REAL NOT NULL DEFAULT 0,
    avg_fill_price REAL,
    fee_usd REAL NOT NULL DEFAULT 0,
    reserved_usd REAL NOT NULL DEFAULT 0,
    reason TEXT,
    parent_client_order_id TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status);
CREATE INDEX IF NOT EXISTS idx_orders_strategy ON orders (strategy_id, symbol);

CREATE TABLE IF NOT EXISTS executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exec_id TEXT NOT NULL UNIQUE,
    client_order_id TEXT NOT NULL,
    exchange_order_id TEXT,
    strategy_id TEXT NOT NULL,
    venue TEXT NOT NULL,
    symbol TEXT NOT NULL,
    mode TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity REAL NOT NULL,
    price REAL NOT NULL,
    fee REAL NOT NULL DEFAULT 0,
    fee_currency TEXT,
    fee_usd REAL NOT NULL DEFAULT 0,
    realized_pnl REAL NOT NULL DEFAULT 0,
    is_maker INTEGER,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exec_ts ON executions (ts);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL,
    venue TEXT NOT NULL,
    symbol TEXT NOT NULL,
    mode TEXT NOT NULL,
    quantity REAL NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    entry_time TEXT,
    exit_time TEXT NOT NULL,
    fees_usd REAL NOT NULL DEFAULT 0,
    realized_pnl REAL NOT NULL,
    exit_reason TEXT,
    exit_client_order_id TEXT NOT NULL,
    is_virtual INTEGER NOT NULL DEFAULT 0,
    UNIQUE (exit_client_order_id, strategy_id)
);
CREATE INDEX IF NOT EXISTS idx_trades_exit ON trades (exit_time);

CREATE TABLE IF NOT EXISTS daily_metrics (
    date TEXT NOT NULL,
    mode TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    start_equity REAL,
    end_equity REAL,
    realized_pnl REAL NOT NULL DEFAULT 0,
    unrealized_pnl REAL NOT NULL DEFAULT 0,
    fees_usd REAL NOT NULL DEFAULT 0,
    trades_count INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    max_drawdown REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (date, mode, strategy_id)
);

CREATE TABLE IF NOT EXISTS risk_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    mode TEXT NOT NULL,
    severity TEXT NOT NULL,
    event_type TEXT NOT NULL,
    strategy_id TEXT,
    symbol TEXT,
    message TEXT NOT NULL,
    details_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_risk_ts ON risk_events (ts);

CREATE TABLE IF NOT EXISTS market_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    venue TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    ts TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    UNIQUE (venue, symbol, timeframe, ts)
);

CREATE TABLE IF NOT EXISTS bot_health (
    strategy_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    mode TEXT NOT NULL,
    last_heartbeat TEXT,
    last_error TEXT,
    restarts INTEGER NOT NULL DEFAULT 0,
    details_json TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS system_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    component TEXT NOT NULL,
    event_type TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_sys_ts ON system_events (ts);

CREATE TABLE IF NOT EXISTS kv_state (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS equity_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    mode TEXT NOT NULL,
    equity REAL NOT NULL,
    cash REAL NOT NULL,
    invested REAL NOT NULL,
    realized_pnl REAL NOT NULL,
    unrealized_pnl REAL NOT NULL,
    drawdown REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_history (mode, ts);

CREATE TABLE IF NOT EXISTS arbitrage_opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    buy_venue TEXT NOT NULL,
    sell_venue TEXT NOT NULL,
    quantity REAL NOT NULL,
    buy_vwap REAL,
    sell_vwap REAL,
    gross_profit REAL,
    fees REAL,
    slippage REAL,
    transfer_cost REAL,
    latency_risk REAL,
    net_profit REAL,
    accepted INTEGER NOT NULL,
    reject_reasons TEXT,
    virtual_executed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_arb_ts ON arbitrage_opportunities (ts);
