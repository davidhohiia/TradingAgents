"""SQLite repository.

* WAL journal + synchronous=FULL: committed transactions survive power loss.
* Explicit transactions (BEGIN IMMEDIATE) for multi-row updates.
* Integrity check at startup; automatic restore from the latest backup if the file is corrupt.
* Thread-safe (single connection guarded by an RLock).
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trinity.core.log import get_logger
from trinity.core.models import (
    ACTIVE_ORDER_STATUSES,
    Fill,
    Mode,
    OrderRecord,
    OrderStatus,
    Position,
    Side,
    utcnow,
)
from trinity.database.models import (
    REQUIRED_TABLES,
    row_to_order,
    row_to_position,
    ts_to_str,
)

log = get_logger(__name__)
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


class DatabaseError(RuntimeError):
    pass


class Repository:
    def __init__(self, path: Path | str, read_only: bool = False) -> None:
        self.path = Path(path) if str(path) != ":memory:" else Path(":memory:")
        self.read_only = read_only
        self._lock = threading.RLock()
        self._tx_depth = 0
        self.conn = self._connect()

    # ------------------------------------------------------------------ connection
    def _connect(self) -> sqlite3.Connection:
        if str(self.path) == ":memory:":
            conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
        elif self.read_only:
            uri = f"file:{self.path.as_posix()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, check_same_thread=False, isolation_level=None)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self.path), check_same_thread=False, isolation_level=None, timeout=10
            )
        conn.row_factory = sqlite3.Row
        if not self.read_only:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            outer = self._tx_depth == 0
            if outer:
                self.conn.execute("BEGIN IMMEDIATE")
            self._tx_depth += 1
            try:
                yield self.conn
            except BaseException:
                self._tx_depth -= 1
                if outer:
                    self.conn.execute("ROLLBACK")
                raise
            else:
                self._tx_depth -= 1
                if outer:
                    self.conn.execute("COMMIT")

    def execute(self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def query(self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute(sql, params).fetchall())

    # ------------------------------------------------------------------ schema
    def migrate(self) -> int:
        with self._lock:
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT)"
            )
            applied = {r[0] for r in self.conn.execute("SELECT version FROM schema_version")}
            count = 0
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                version = int(path.name.split("_", 1)[0])
                if version in applied:
                    continue
                sql = path.read_text(encoding="utf-8")
                with self.transaction() as conn:
                    for stmt in _split_sql(sql):
                        conn.execute(stmt)
                    conn.execute(
                        "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                        (version, utcnow().isoformat()),
                    )
                count += 1
                log.info("applied migration %s", path.name)
            return count

    def integrity_ok(self) -> bool:
        try:
            with self._lock:
                result = self.conn.execute("PRAGMA integrity_check").fetchone()
            return bool(result) and result[0] == "ok"
        except sqlite3.DatabaseError:
            return False

    def missing_tables(self) -> list[str]:
        rows = self.query("SELECT name FROM sqlite_master WHERE type='table'")
        names = {r[0] for r in rows}
        return [t for t in REQUIRED_TABLES if t not in names]

    def backup_to(self, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            target = sqlite3.connect(str(dest))
            try:
                self.conn.backup(target)
            finally:
                target.close()
        return dest

    # ------------------------------------------------------------------ kv state
    def get_kv(self, key: str, default: Any = None) -> Any:
        rows = self.query("SELECT value_json FROM kv_state WHERE key=?", (key,))
        if not rows:
            return default
        return json.loads(rows[0][0])

    def set_kv(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO kv_state (key, value_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
            (key, json.dumps(value, default=str), utcnow().isoformat()),
        )

    def delete_kv(self, key: str) -> None:
        self.execute("DELETE FROM kv_state WHERE key=?", (key,))

    # ------------------------------------------------------------------ strategies / accounts
    def upsert_strategy(self, strategy_id: str, name: str, bot_type: str, allocation: float, enabled: bool) -> None:
        now = utcnow().isoformat()
        self.execute(
            "INSERT INTO strategies (strategy_id, name, bot_type, allocation_usd, enabled, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(strategy_id) DO UPDATE SET name=excluded.name, "
            "bot_type=excluded.bot_type, allocation_usd=excluded.allocation_usd, enabled=excluded.enabled, "
            "updated_at=excluded.updated_at",
            (strategy_id, name, bot_type, allocation, int(enabled), now, now),
        )

    def upsert_account(self, venue: str, mode: Mode, currency: str, description: str) -> None:
        self.execute(
            "INSERT INTO accounts (venue, mode, currency, description, created_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(venue, mode) DO UPDATE SET currency=excluded.currency, description=excluded.description",
            (venue, mode.value, currency, description, utcnow().isoformat()),
        )

    def upsert_balance(self, venue: str, mode: Mode, asset: str, free: float, used: float, usd_value: float | None) -> None:
        self.execute(
            "INSERT INTO balances (venue, mode, asset, free, used, total, usd_value, ts) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(venue, mode, asset) DO UPDATE SET free=excluded.free, used=excluded.used, "
            "total=excluded.total, usd_value=excluded.usd_value, ts=excluded.ts",
            (venue, mode.value, asset, free, used, free + used, usd_value, utcnow().isoformat()),
        )

    # ------------------------------------------------------------------ orders
    def insert_order(self, o: OrderRecord) -> None:
        self.execute(
            "INSERT INTO orders (client_order_id, exchange_order_id, strategy_id, venue, symbol, mode, side, "
            "order_type, quantity, price, stop_price, take_profit, reduce_only, status, filled_quantity, "
            "avg_fill_price, fee_usd, reserved_usd, reason, parent_client_order_id, error, created_at, updated_at, "
            "sub_strategy, leverage) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                o.client_order_id, o.exchange_order_id, o.strategy_id, o.venue, o.symbol, o.mode.value,
                o.side.value, o.order_type.value, o.quantity, o.price, o.stop_price, o.take_profit,
                int(o.reduce_only), o.status.value, o.filled_quantity, o.avg_fill_price, o.fee_usd,
                o.reserved_usd, o.reason, o.parent_client_order_id, o.error,
                ts_to_str(o.created_at), ts_to_str(o.updated_at), o.sub_strategy, o.leverage,
            ),
        )

    def update_order(self, o: OrderRecord) -> None:
        o.updated_at = utcnow()
        self.execute(
            "UPDATE orders SET exchange_order_id=?, status=?, filled_quantity=?, avg_fill_price=?, fee_usd=?, "
            "reserved_usd=?, error=?, updated_at=? WHERE client_order_id=?",
            (
                o.exchange_order_id, o.status.value, o.filled_quantity, o.avg_fill_price, o.fee_usd,
                o.reserved_usd, o.error, ts_to_str(o.updated_at), o.client_order_id,
            ),
        )

    def get_order(self, client_order_id: str) -> OrderRecord | None:
        rows = self.query("SELECT * FROM orders WHERE client_order_id=?", (client_order_id,))
        return row_to_order(rows[0]) if rows else None

    def order_exists(self, client_order_id: str) -> bool:
        return bool(self.query("SELECT 1 FROM orders WHERE client_order_id=?", (client_order_id,)))

    def list_active_orders(
        self, mode: Mode | None = None, strategy_id: str | None = None, symbol: str | None = None,
        venue: str | None = None,
    ) -> list[OrderRecord]:
        sql = "SELECT * FROM orders WHERE status IN ({})".format(
            ",".join("?" * len(ACTIVE_ORDER_STATUSES))
        )
        params: list[Any] = [s.value for s in ACTIVE_ORDER_STATUSES]
        for col, val in (("mode", mode.value if mode else None), ("strategy_id", strategy_id),
                         ("symbol", symbol), ("venue", venue)):
            if val is not None:
                sql += f" AND {col}=?"
                params.append(val)
        sql += " ORDER BY id"
        return [row_to_order(r) for r in self.query(sql, tuple(params))]

    def list_orders(self, mode: Mode | None = None, limit: int = 100, strategy_id: str | None = None) -> list[OrderRecord]:
        sql = "SELECT * FROM orders WHERE 1=1"
        params: list[Any] = []
        if mode:
            sql += " AND mode=?"
            params.append(mode.value)
        if strategy_id:
            sql += " AND strategy_id=?"
            params.append(strategy_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [row_to_order(r) for r in self.query(sql, tuple(params))]

    def count_orders_since(self, since: datetime, mode: Mode) -> int:
        rows = self.query(
            "SELECT COUNT(*) FROM orders WHERE created_at>=? AND mode=?", (ts_to_str(since), mode.value)
        )
        return int(rows[0][0])

    # ------------------------------------------------------------------ executions
    def insert_execution(self, f: Fill) -> bool:
        """Insert a fill. Returns False if exec_id already exists (duplicate protection)."""
        try:
            self.execute(
                "INSERT INTO executions (exec_id, client_order_id, exchange_order_id, strategy_id, venue, symbol, "
                "mode, side, quantity, price, fee, fee_currency, fee_usd, realized_pnl, is_maker, ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f.exec_id, f.client_order_id, f.exchange_order_id, f.strategy_id, f.venue, f.symbol,
                    f.mode.value, f.side.value, f.quantity, f.price, f.fee, f.fee_currency, f.fee_usd,
                    f.realized_pnl, None if f.is_maker is None else int(f.is_maker), ts_to_str(f.ts),
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def execution_exists(self, exec_id: str) -> bool:
        return bool(self.query("SELECT 1 FROM executions WHERE exec_id=?", (exec_id,)))

    def list_executions(self, mode: Mode | None = None, limit: int = 100) -> list[sqlite3.Row]:
        if mode:
            return self.query("SELECT * FROM executions WHERE mode=? ORDER BY id DESC LIMIT ?", (mode.value, limit))
        return self.query("SELECT * FROM executions ORDER BY id DESC LIMIT ?", (limit,))

    # ------------------------------------------------------------------ positions
    def upsert_position(self, p: Position) -> None:
        self.execute(
            "INSERT INTO positions (strategy_id, venue, symbol, mode, asset_class, correlation_group, quantity, "
            "avg_entry_price, realized_pnl, fees_paid, last_price, unrealized_pnl, stop_price, take_profit, "
            "opened_at, updated_at, sub_strategy, leverage, liquidation_price, funding_paid, cycle_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(strategy_id, venue, symbol, mode) DO UPDATE SET quantity=excluded.quantity, "
            "avg_entry_price=excluded.avg_entry_price, realized_pnl=excluded.realized_pnl, "
            "fees_paid=excluded.fees_paid, last_price=excluded.last_price, unrealized_pnl=excluded.unrealized_pnl, "
            "stop_price=excluded.stop_price, take_profit=excluded.take_profit, opened_at=excluded.opened_at, "
            "updated_at=excluded.updated_at, correlation_group=excluded.correlation_group, "
            "sub_strategy=excluded.sub_strategy, leverage=excluded.leverage, "
            "liquidation_price=excluded.liquidation_price, funding_paid=excluded.funding_paid, "
            "cycle_json=excluded.cycle_json",
            (
                p.strategy_id, p.venue, p.symbol, p.mode.value, p.asset_class.value, p.correlation_group,
                p.quantity, p.avg_entry_price, p.realized_pnl, p.fees_paid, p.last_price, p.unrealized_pnl,
                p.stop_price, p.take_profit, ts_to_str(p.opened_at), ts_to_str(p.updated_at),
                p.sub_strategy, p.leverage, p.liquidation_price, p.funding_paid,
                json.dumps(p.cycle) if p.cycle else None,
            ),
        )

    def list_positions(self, mode: Mode | None = None, open_only: bool = False) -> list[Position]:
        sql = "SELECT * FROM positions WHERE 1=1"
        params: list[Any] = []
        if mode:
            sql += " AND mode=?"
            params.append(mode.value)
        if open_only:
            sql += " AND quantity > 1e-12"
        return [row_to_position(r) for r in self.query(sql, tuple(params))]

    # ------------------------------------------------------------------ trades
    def upsert_trade_fill(
        self, *, strategy_id: str, venue: str, symbol: str, mode: Mode, quantity: float, entry_price: float,
        exit_price: float, entry_time: datetime | None, exit_time: datetime, fees_usd: float,
        realized_pnl: float, exit_reason: str, exit_client_order_id: str, is_virtual: bool,
        sub_strategy: str = "",
    ) -> None:
        """Aggregate all partial fills of one exit order into a single round-trip trade row."""
        rows = self.query(
            "SELECT quantity, exit_price, fees_usd, realized_pnl, entry_price FROM trades "
            "WHERE exit_client_order_id=? AND strategy_id=?",
            (exit_client_order_id, strategy_id),
        )
        if rows:
            r = rows[0]
            q_old = float(r["quantity"])
            q_new = q_old + quantity
            exit_avg = (float(r["exit_price"]) * q_old + exit_price * quantity) / q_new
            entry_avg = (float(r["entry_price"]) * q_old + entry_price * quantity) / q_new
            self.execute(
                "UPDATE trades SET quantity=?, exit_price=?, entry_price=?, fees_usd=?, realized_pnl=?, exit_time=? "
                "WHERE exit_client_order_id=? AND strategy_id=?",
                (q_new, exit_avg, entry_avg, float(r["fees_usd"]) + fees_usd,
                 float(r["realized_pnl"]) + realized_pnl, ts_to_str(exit_time), exit_client_order_id, strategy_id),
            )
        else:
            self.execute(
                "INSERT INTO trades (strategy_id, venue, symbol, mode, quantity, entry_price, exit_price, entry_time, "
                "exit_time, fees_usd, realized_pnl, exit_reason, exit_client_order_id, is_virtual, sub_strategy) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (strategy_id, venue, symbol, mode.value, quantity, entry_price, exit_price, ts_to_str(entry_time),
                 ts_to_str(exit_time), fees_usd, realized_pnl, exit_reason, exit_client_order_id, int(is_virtual),
                 sub_strategy),
            )

    def list_trades(self, mode: Mode | None = None, limit: int = 100, strategy_id: str | None = None,
                    since: datetime | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM trades WHERE 1=1"
        params: list[Any] = []
        if mode:
            sql += " AND mode=?"
            params.append(mode.value)
        if strategy_id:
            sql += " AND strategy_id=?"
            params.append(strategy_id)
        if since:
            sql += " AND exit_time>=?"
            params.append(ts_to_str(since))
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return self.query(sql, tuple(params))

    def trade_stats(self, mode: Mode, strategy_id: str | None = None, since: datetime | None = None,
                    sub_strategy: str | None = None) -> dict[str, float]:
        sql = ("SELECT COUNT(*) n, COALESCE(SUM(realized_pnl),0) pnl, COALESCE(SUM(fees_usd),0) fees, "
               "COALESCE(SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END),0) wins, "
               "COALESCE(SUM(CASE WHEN realized_pnl<=0 THEN 1 ELSE 0 END),0) losses, "
               "COALESCE(SUM(CASE WHEN realized_pnl>0 THEN realized_pnl ELSE 0 END),0) gross_win, "
               "COALESCE(SUM(CASE WHEN realized_pnl<0 THEN -realized_pnl ELSE 0 END),0) gross_loss, "
               "MIN(exit_time) first_exit FROM trades WHERE mode=?")
        params: list[Any] = [mode.value]
        if strategy_id:
            sql += " AND strategy_id=?"
            params.append(strategy_id)
        if since:
            sql += " AND exit_time>=?"
            params.append(ts_to_str(since))
        if sub_strategy is not None:
            sql += " AND sub_strategy=?"
            params.append(sub_strategy)
        r = self.query(sql, tuple(params))[0]
        return {k: r[k] for k in r.keys()}

    def realized_pnl_by_strategy(self, mode: Mode) -> dict[str, float]:
        rows = self.query(
            "SELECT strategy_id, COALESCE(SUM(realized_pnl),0) FROM positions WHERE mode=? GROUP BY strategy_id",
            (mode.value,),
        )
        return {r[0]: float(r[1]) for r in rows}

    # ------------------------------------------------------------------ events / health / metrics
    def add_risk_event(self, mode: Mode, severity: str, event_type: str, message: str,
                       strategy_id: str | None = None, symbol: str | None = None,
                       details: dict[str, Any] | None = None, ts: datetime | None = None) -> None:
        self.execute(
            "INSERT INTO risk_events (ts, mode, severity, event_type, strategy_id, symbol, message, details_json) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (ts_to_str(ts or utcnow()), mode.value, severity, event_type, strategy_id, symbol, message,
             json.dumps(details or {}, default=str)),
        )

    def list_risk_events(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM risk_events ORDER BY id DESC LIMIT ?", (limit,))

    def add_system_event(self, level: str, component: str, event_type: str, message: str,
                         details: dict[str, Any] | None = None, ts: datetime | None = None) -> None:
        self.execute(
            "INSERT INTO system_events (ts, level, component, event_type, message, details_json) VALUES (?,?,?,?,?,?)",
            (ts_to_str(ts or utcnow()), level, component, event_type, message, json.dumps(details or {}, default=str)),
        )

    def list_system_events(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM system_events ORDER BY id DESC LIMIT ?", (limit,))

    def upsert_bot_health(self, strategy_id: str, status: str, mode: Mode, last_error: str | None,
                          restarts: int, details: dict[str, Any] | None = None, ts: datetime | None = None) -> None:
        now = ts_to_str(ts or utcnow())
        self.execute(
            "INSERT INTO bot_health (strategy_id, status, mode, last_heartbeat, last_error, restarts, details_json, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(strategy_id) DO UPDATE SET status=excluded.status, "
            "mode=excluded.mode, last_heartbeat=excluded.last_heartbeat, last_error=excluded.last_error, "
            "restarts=excluded.restarts, details_json=excluded.details_json, updated_at=excluded.updated_at",
            (strategy_id, status, mode.value, now, last_error, restarts, json.dumps(details or {}, default=str), now),
        )

    def list_bot_health(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM bot_health ORDER BY strategy_id")

    def add_equity_snapshot(self, ts: datetime, mode: Mode, equity: float, cash: float, invested: float,
                            realized: float, unrealized: float, drawdown: float) -> None:
        self.execute(
            "INSERT INTO equity_history (ts, mode, equity, cash, invested, realized_pnl, unrealized_pnl, drawdown) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (ts_to_str(ts), mode.value, equity, cash, invested, realized, unrealized, drawdown),
        )

    def equity_history(self, mode: Mode, limit: int = 5000) -> list[sqlite3.Row]:
        rows = self.query(
            "SELECT * FROM equity_history WHERE mode=? ORDER BY id DESC LIMIT ?", (mode.value, limit)
        )
        return list(reversed(rows))

    def first_equity_ts(self, mode: Mode) -> datetime | None:
        rows = self.query("SELECT MIN(ts) FROM equity_history WHERE mode=?", (mode.value,))
        return datetime.fromisoformat(rows[0][0]).astimezone(UTC) if rows and rows[0][0] else None

    def upsert_daily_metrics(self, date: str, mode: Mode, strategy_id: str, **values: float) -> None:
        cols = ["start_equity", "end_equity", "realized_pnl", "unrealized_pnl", "fees_usd", "trades_count",
                "wins", "losses", "max_drawdown"]
        data = {c: values.get(c) for c in cols if c in values}
        existing = self.query(
            "SELECT * FROM daily_metrics WHERE date=? AND mode=? AND strategy_id=?", (date, mode.value, strategy_id)
        )
        now = utcnow().isoformat()
        if existing:
            sets = ", ".join(f"{k}=?" for k in data) + (", " if data else "") + "updated_at=?"
            self.execute(
                f"UPDATE daily_metrics SET {sets} WHERE date=? AND mode=? AND strategy_id=?",
                (*data.values(), now, date, mode.value, strategy_id),
            )
        else:
            keys = ["date", "mode", "strategy_id", *data.keys(), "updated_at"]
            self.execute(
                f"INSERT INTO daily_metrics ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
                (date, mode.value, strategy_id, *data.values(), now),
            )

    def list_daily_metrics(self, mode: Mode, limit: int = 60) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM daily_metrics WHERE mode=? ORDER BY date DESC LIMIT ?", (mode.value, limit)
        )

    def insert_candles(self, venue: str, symbol: str, timeframe: str,
                       candles: list[tuple[str, float, float, float, float, float]]) -> None:
        with self.transaction() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO market_data (venue, symbol, timeframe, ts, open, high, low, close, volume) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                [(venue, symbol, timeframe, *c) for c in candles],
            )

    def add_arbitrage_opportunity(self, row: dict[str, Any]) -> None:
        keys = list(row.keys())
        self.execute(
            f"INSERT INTO arbitrage_opportunities ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
            tuple(row[k] for k in keys),
        )

    def list_arbitrage_opportunities(self, limit: int = 100, accepted_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM arbitrage_opportunities"
        if accepted_only:
            sql += " WHERE accepted=1"
        sql += " ORDER BY id DESC LIMIT ?"
        return self.query(sql, (limit,))

    # ------------------------------------------------------------------ trade journal / Google Sheets outbox
    def insert_journal_trade(self, t: dict[str, Any], ts: datetime) -> bool:
        """Insert a closed trade AND its outbox entry (call inside the fill transaction).
        Returns False if the trade id already exists (no duplicates)."""
        cols = ["trade_id", "mode", "strategy_id", "venue", "symbol", "direction", "sub_strategy", "quantity",
                "entry_price", "exit_price", "entry_time", "exit_time", "fees", "funding", "gross_pnl", "net_pnl",
                "net_pct", "capital_employed", "leverage", "status", "close_reason", "entry_fills", "exit_fills"]
        try:
            with self.transaction():
                self.execute(
                    f"INSERT INTO journal_trades ({', '.join(cols)}, created_at) VALUES ({', '.join('?' * (len(cols) + 1))})",
                    (*(t[c] for c in cols), ts_to_str(ts)))
                self.execute(
                    "INSERT INTO sheets_outbox (kind, record_key, payload_json, status, created_at) "
                    "VALUES ('trade', ?, ?, 'pending', ?)", (t["trade_id"], json.dumps(t), ts_to_str(ts)))
            return True
        except sqlite3.IntegrityError:
            return False

    def list_journal_trades(self, mode: Mode | None = None) -> list[dict[str, Any]]:
        if mode:
            rows = self.query("SELECT * FROM journal_trades WHERE mode=? ORDER BY exit_time", (mode.value,))
        else:
            rows = self.query("SELECT * FROM journal_trades ORDER BY exit_time")
        return [dict(r) for r in rows]

    def pending_outbox(self, kind: str = "trade", limit: int = 500) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM sheets_outbox WHERE status='pending' AND kind=? ORDER BY id LIMIT ?",
                          (kind, limit))

    def mark_outbox_sent(self, ids: list[int], ts: datetime) -> None:
        with self.transaction():
            for i in ids:
                self.execute("UPDATE sheets_outbox SET status='sent', sent_at=?, last_error=NULL WHERE id=?",
                             (ts_to_str(ts), i))

    def mark_outbox_failed(self, ids: list[int], error: str) -> None:
        with self.transaction():
            for i in ids:
                self.execute("UPDATE sheets_outbox SET attempts=attempts+1, last_error=? WHERE id=?",
                             (error[:500], i))

    def outbox_stats(self) -> dict[str, int]:
        rows = self.query("SELECT status, COUNT(*) n FROM sheets_outbox GROUP BY status")
        out = {"pending": 0, "sent": 0}
        out.update({r["status"]: int(r["n"]) for r in rows})
        return out

    def add_sync_error(self, ts: datetime, target: str, operation: str, error: str, retryable: bool) -> None:
        self.execute("INSERT INTO sync_errors (ts, target, operation, error, retryable) VALUES (?,?,?,?,?)",
                     (ts_to_str(ts), target, operation, error[:1000], int(retryable)))

    def list_sync_errors(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM sync_errors ORDER BY id DESC LIMIT ?", (limit,))

    # ------------------------------------------------------------------ capital growth / monitoring
    def add_deposit(self, ts: datetime, amount_usd: float, kind: str, note: str = "",
                     currency: str = "USD", amount_original: float | None = None, fx_rate: float = 1.0) -> int:
        cur = self.execute(
            "INSERT INTO deposits (ts, amount_usd, currency, amount_original, fx_rate, kind, note) VALUES (?,?,?,?,?,?,?)",
            (ts_to_str(ts), amount_usd, currency, amount_original if amount_original is not None else amount_usd,
             fx_rate, kind, note),
        )
        return int(cur.lastrowid)


    def list_deposits(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM deposits ORDER BY id")


    def add_allocation_change(self, ts: datetime, strategy_id: str, delta: float, new_amount: float,
                               reason: str, approved_by: str) -> None:
        self.execute(
            "INSERT INTO capital_allocations (ts, strategy_id, delta_usd, new_amount_usd, reason, approved_by) "
            "VALUES (?,?,?,?,?,?)", (ts_to_str(ts), strategy_id, delta, new_amount, reason, approved_by))


    def add_api_health(self, ts: datetime, venue: str, calls: int, errors: int, p50: float | None,
                        p95: float | None, last_error: str | None) -> None:
        self.execute("INSERT INTO api_health (ts, venue, calls, errors, p50_ms, p95_ms, last_error) VALUES (?,?,?,?,?,?,?)",
                     (ts_to_str(ts), venue, calls, errors, p50, p95, last_error))


    def add_funding_payment(self, ts: datetime, mode: Mode, strategy_id: str, symbol: str, rate: float,
                             notional: float, amount: float) -> None:
        self.execute("INSERT INTO funding_payments (ts, mode, strategy_id, symbol, rate, notional, amount_usd) "
                     "VALUES (?,?,?,?,?,?,?)", (ts_to_str(ts), mode.value, strategy_id, symbol, rate, notional, amount))


def _split_sql(sql: str) -> list[str]:
    lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


# ---------------------------------------------------------------------- recovery
def latest_backup(backup_dir: Path) -> Path | None:
    if not backup_dir.exists():
        return None
    backups = sorted(backup_dir.glob("trinity-*.db"))
    return backups[-1] if backups else None


def open_database(path: Path, backup_dir: Path) -> tuple[Repository, list[str]]:
    """Open + migrate the database, recovering from corruption.

    Returns (repository, notes). If a restore happened, the kv flag
    ``recovery_required`` is set so trading stays locked until reconciliation.
    """
    notes: list[str] = []
    repo: Repository | None = None
    try:
        repo = Repository(path)
        if not repo.integrity_ok():
            raise DatabaseError("integrity check failed")
        repo.migrate()
        return repo, notes
    except (sqlite3.DatabaseError, DatabaseError) as exc:
        notes.append(f"database problem detected: {exc}")
        if repo is not None:
            repo.close()
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        corrupt = path.with_name(f"{path.stem}.corrupt-{stamp}{path.suffix}")
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(path) + suffix)
            if src.exists():
                shutil.move(str(src), str(corrupt) + suffix)
        notes.append(f"corrupt database moved to {corrupt.name}")
        backup = latest_backup(backup_dir)
        if backup is not None:
            shutil.copy2(backup, path)
            notes.append(f"restored from backup {backup.name}")
        else:
            notes.append("no backup found: starting with an empty database")
        repo = Repository(path)
        repo.migrate()
        repo.set_kv("recovery_required", {"reason": "; ".join(notes), "ts": utcnow().isoformat()})
        repo.add_system_event("CRITICAL", "database", "recovery", "; ".join(notes))
        return repo, notes


def side_sign(side: Side) -> int:
    return 1 if side == Side.BUY else -1


__all__ = ["Repository", "open_database", "latest_backup", "DatabaseError", "OrderStatus"]
