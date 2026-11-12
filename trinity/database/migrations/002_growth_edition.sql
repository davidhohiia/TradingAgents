-- TRINITY 3.0: sub-strategies, leverage fields, capital growth, API health, funding.

ALTER TABLE orders ADD COLUMN sub_strategy TEXT NOT NULL DEFAULT '';
ALTER TABLE orders ADD COLUMN leverage REAL NOT NULL DEFAULT 1;
ALTER TABLE trades ADD COLUMN sub_strategy TEXT NOT NULL DEFAULT '';
ALTER TABLE positions ADD COLUMN sub_strategy TEXT NOT NULL DEFAULT '';
ALTER TABLE positions ADD COLUMN leverage REAL NOT NULL DEFAULT 1;
ALTER TABLE positions ADD COLUMN liquidation_price REAL;
ALTER TABLE positions ADD COLUMN funding_paid REAL NOT NULL DEFAULT 0;

-- Every contribution (the initial capital included). Deposits are NEVER income.
CREATE TABLE IF NOT EXISTS deposits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    amount_usd REAL NOT NULL CHECK (amount_usd > 0),
    currency TEXT NOT NULL DEFAULT 'USD',
    amount_original REAL NOT NULL,
    fx_rate REAL NOT NULL DEFAULT 1,
    kind TEXT NOT NULL CHECK (kind IN ('initial', 'monthly', 'other')),
    note TEXT
);

-- Audit trail of every allocation change (initial, approved reallocation).
CREATE TABLE IF NOT EXISTS capital_allocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    delta_usd REAL NOT NULL,
    new_amount_usd REAL NOT NULL,
    reason TEXT NOT NULL,
    approved_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reallocation_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    amount_usd REAL NOT NULL,
    rationale TEXT NOT NULL,
    evidence_json TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
    decided_at TEXT,
    decision_note TEXT
);

CREATE TABLE IF NOT EXISTS api_health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    venue TEXT NOT NULL,
    calls INTEGER NOT NULL,
    errors INTEGER NOT NULL,
    p50_ms REAL,
    p95_ms REAL,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_health ON api_health (venue, ts);

CREATE TABLE IF NOT EXISTS funding_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    mode TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    rate REAL NOT NULL,
    notional REAL NOT NULL,
    amount_usd REAL NOT NULL
);
