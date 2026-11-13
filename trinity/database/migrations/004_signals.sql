-- TRINITY 4.0: every evaluated entry signal with its Trade Quality Score and the system decision.

ALTER TABLE orders ADD COLUMN quality_score REAL;

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    mode TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    sub_strategy TEXT NOT NULL DEFAULT '',
    symbol TEXT NOT NULL,
    score INTEGER NOT NULL,
    grade TEXT NOT NULL,
    threshold INTEGER NOT NULL,
    decision TEXT NOT NULL,
    reasons TEXT,
    components_json TEXT,
    client_order_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals (mode, ts);
