-- Trade journal (one row per fully closed position cycle) + durable outbox for Google Sheets.
-- SQLite stays the primary database; Google Sheets is only a secondary journal.

ALTER TABLE positions ADD COLUMN cycle_json TEXT;

CREATE TABLE IF NOT EXISTS journal_trades (
    trade_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    venue TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
    sub_strategy TEXT NOT NULL DEFAULT '',
    quantity REAL NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    entry_time TEXT NOT NULL,
    exit_time TEXT NOT NULL,
    fees REAL NOT NULL,
    funding REAL NOT NULL DEFAULT 0,
    gross_pnl REAL NOT NULL,
    net_pnl REAL NOT NULL,
    net_pct REAL NOT NULL,
    capital_employed REAL NOT NULL,
    leverage REAL NOT NULL DEFAULT 1,
    status TEXT NOT NULL,
    close_reason TEXT,
    entry_fills INTEGER NOT NULL,
    exit_fills INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_journal_exit ON journal_trades (mode, exit_time);

CREATE TABLE IF NOT EXISTS sheets_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    record_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'sent')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    UNIQUE (kind, record_key)
);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON sheets_outbox (status, id);

CREATE TABLE IF NOT EXISTS sync_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    target TEXT NOT NULL,
    operation TEXT NOT NULL,
    error TEXT NOT NULL,
    retryable INTEGER NOT NULL DEFAULT 1
);
