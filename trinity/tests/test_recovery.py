"""Crash recovery, DB restore, reconciliation, process lock, paper/live separation and the live gate."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from trinity.config.settings import LIVE_ACKNOWLEDGEMENT_PHRASE, Settings
from trinity.core.app import build_app, startup_reconcile
from trinity.core.live_guard import evaluate_static_gate, source_hash
from trinity.core.models import AssetClass, Mode, OrderIntent, OrderStatus, OrderType, Side
from trinity.database.repository import Repository, open_database
from trinity.monitoring.process_lock import AlreadyRunning, ProcessLock
from trinity.scripts.backup import create_backup, restore_backup
from trinity.tests.conftest import START, build_system


def settings_for(tmp_path: Path, **kw) -> Settings:
    base = dict(database_path=tmp_path / "t.db", backup_dir=tmp_path / "bk", log_dir=tmp_path / "logs",
                lock_file=tmp_path / "t.lock", telegram_bot_token=None, crypto_api_key=None, crypto_api_secret=None,
                alpaca_api_key_id=None, alpaca_api_secret_key=None, live_trading=False, paper_trading=True,
                live_trading_acknowledgement="")
    base.update(kw)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


# ---------------------------------------------------------------- database
def test_transaction_rollback(tmp_path):
    repo = Repository(tmp_path / "r.db")
    repo.migrate()
    with pytest.raises(RuntimeError), repo.transaction():
        repo.set_kv("a", 1)
        raise RuntimeError("crash mid-transaction")
    assert repo.get_kv("a") is None
    with repo.transaction():
        repo.set_kv("a", 2)
    assert repo.get_kv("a") == 2


def test_schema_has_all_required_tables(tmp_path):
    repo = Repository(tmp_path / "r.db")
    repo.migrate()
    assert repo.missing_tables() == []
    assert repo.migrate() == 0  # idempotent


def test_corrupt_database_restored_from_backup(tmp_path):
    db, bk = tmp_path / "t.db", tmp_path / "bk"
    repo, notes = open_database(db, bk)
    repo.set_kv("important", {"v": 42})
    create_backup(repo, bk)
    repo.close()
    for suffix in ("-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    db.write_bytes(b"this is not a sqlite database" * 100)
    repo2, notes = open_database(db, bk)
    assert any("restored from backup" in n for n in notes)
    assert repo2.get_kv("important") == {"v": 42}
    assert repo2.get_kv("recovery_required") is not None
    assert list(tmp_path.glob("t.corrupt-*"))


def test_corrupt_database_without_backup_starts_empty_and_locked(tmp_path):
    db = tmp_path / "t.db"
    db.write_bytes(b"garbage" * 500)
    repo, notes = open_database(db, tmp_path / "none")
    assert any("no backup" in n for n in notes)
    assert repo.missing_tables() == [] and repo.get_kv("recovery_required")


def test_backup_restore_roundtrip(tmp_path):
    repo = Repository(tmp_path / "a.db")
    repo.migrate()
    repo.set_kv("x", 1)
    b = create_backup(repo, tmp_path / "bk")
    repo.set_kv("x", 2)
    repo.close()
    restore_backup(b, tmp_path / "a.db")
    r2 = Repository(tmp_path / "a.db")
    assert r2.get_kv("x") == 1 and r2.get_kv("recovery_required")


def test_committed_data_survives_unclean_shutdown(tmp_path):
    repo = Repository(tmp_path / "w.db")
    repo.migrate()
    repo.set_kv("k", "committed")
    # simulate a crash: connection never closed, then a new process opens the file
    other = sqlite3.connect(str(tmp_path / "w.db"))
    assert json.loads(other.execute("SELECT value_json FROM kv_state WHERE key='k'").fetchone()[0]) == "committed"
    other.close()


# ---------------------------------------------------------------- crash during order submission
async def test_crash_after_send_before_ack_is_reconciled(tmp_path, config):
    s = build_system(tmp_path, config)
    b = await s.crypto_market.fetch_order_book("BTC/USDT")
    intent = OrderIntent(strategy_id="crypto_grid", venue="crypto", symbol="BTC/USDT", side=Side.BUY,
                         order_type=OrderType.LIMIT, quantity=0.0003, reference_price=b.best_ask,
                         price=round(b.best_bid * 0.99, 2), stop_price=b.best_bid * 0.95, market_data_ts=b.ts,
                         asset_class=AssetClass.CRYPTO)
    original = s.crypto.create_order

    async def send_then_crash(req):
        await original(req)
        raise SystemExit("power loss")  # process dies after the venue accepted the order

    s.crypto.create_order = send_then_crash
    with pytest.raises(SystemExit):
        await s.engine.submit(intent)
    assert s.repo.get_order(intent.client_order_id).status == OrderStatus.PENDING_SUBMIT  # write-ahead record
    paper_state = s.crypto.to_state()
    # "restart": new engine on the same DB file, venue state as it was on the exchange
    s2 = build_system(tmp_path, config, db_path=tmp_path / "test.db")
    s2.crypto.load_state(paper_state)
    rep = await s2.engine.reconcile("crypto")
    assert rep.ok, rep.issues
    o = s2.repo.get_order(intent.client_order_id)
    assert o.status == OrderStatus.OPEN and o.exchange_order_id


async def test_crash_before_send_marks_order_failed(tmp_path, config):
    s = build_system(tmp_path, config)
    b = await s.crypto_market.fetch_order_book("BTC/USDT")

    async def crash(req):
        raise SystemExit("crash before network call")

    s.crypto.create_order = crash
    intent = OrderIntent(strategy_id="crypto_grid", venue="crypto", symbol="BTC/USDT", side=Side.BUY,
                         order_type=OrderType.MARKET, quantity=0.0003, reference_price=b.best_ask,
                         stop_price=b.best_ask * 0.97, market_data_ts=b.ts, asset_class=AssetClass.CRYPTO)
    with pytest.raises(SystemExit):
        await s.engine.submit(intent)
    s2 = build_system(tmp_path, config, db_path=tmp_path / "test.db")
    rep = await s2.engine.reconcile("crypto")
    assert rep.ok
    o = s2.repo.get_order(intent.client_order_id)
    assert o.status == OrderStatus.FAILED and o.reserved_usd == 0


async def test_positions_and_risk_state_survive_restart(tmp_path, config):
    s = build_system(tmp_path, config)
    b = await s.crypto_market.fetch_order_book("BTC/USDT")
    await s.engine.submit(OrderIntent(strategy_id="crypto_grid", venue="crypto", symbol="BTC/USDT", side=Side.BUY,
                                      order_type=OrderType.MARKET, quantity=0.0003, reference_price=b.best_ask,
                                      stop_price=b.best_ask * 0.97, market_data_ts=b.ts, asset_class=AssetClass.CRYPTO))
    s.risk.emergency_stop("test")
    s2 = build_system(tmp_path, config, db_path=tmp_path / "test.db")
    assert s2.positions.get("crypto_grid", "crypto", "BTC/USDT").quantity == pytest.approx(0.0003)
    assert "emergency_stop" in s2.risk.blocking_locks()


# ---------------------------------------------------------------- app-level recovery
async def test_app_paper_state_persists_and_reconcile_clears_recovery(tmp_path, config):
    s = build_system(tmp_path, config)
    st = settings_for(tmp_path)
    repo = Repository(tmp_path / "app.db")
    repo.migrate()
    repo.set_kv("recovery_required", {"reason": "test"})
    app = build_app(st, config, repo, Mode.PAPER, clock=s.clock, crypto_data=s.crypto_market,
                    stock_data=s.stock_market, arb_sources={})
    app.engine.risk.set_recovery_lock("db restored")
    assert app.mode == Mode.PAPER and all(v.is_paper for v in app.venues.values())
    reports = await startup_reconcile(app)
    assert all(r.ok for r in reports)
    assert "recovery_lock" not in app.engine.risk.blocking_locks()
    assert repo.get_kv("recovery_required") is None
    app.venues["crypto"].balances["USDT"].free = 123.0
    app.venues["crypto"]._changed()
    app2 = build_app(st, config, repo, Mode.PAPER, clock=s.clock, crypto_data=s.crypto_market,
                     stock_data=s.stock_market, arb_sources={})
    assert app2.venues["crypto"].balances["USDT"].free == 123.0


async def test_failed_reconciliation_locks_trading(tmp_path, config):
    s = build_system(tmp_path, config)
    repo = Repository(tmp_path / "app.db")
    repo.migrate()
    app = build_app(settings_for(tmp_path), config, repo, Mode.PAPER, clock=s.clock, crypto_data=s.crypto_market,
                    stock_data=s.stock_market, arb_sources={})
    from trinity.exchanges.base_exchange import ExchangeUnavailable

    app.venues["crypto"].inject_failure("fetch_open_orders", ExchangeUnavailable("down"))
    await startup_reconcile(app)
    assert "recovery_lock" in app.engine.risk.blocking_locks()


# ---------------------------------------------------------------- single instance
def test_process_lock_prevents_second_instance(tmp_path):
    a = ProcessLock(tmp_path / "x.lock")
    a.acquire()
    with pytest.raises(AlreadyRunning):
        ProcessLock(tmp_path / "x.lock").acquire()
    a.release()
    b = ProcessLock(tmp_path / "x.lock")
    b.acquire()
    b.release()


# ---------------------------------------------------------------- paper / live separation + live gate
def test_default_settings_are_paper_only(tmp_path):
    st = settings_for(tmp_path)
    assert st.paper_trading and not st.live_trading


def test_live_gate_fails_by_default(tmp_path, config):
    repo = Repository(tmp_path / "g.db")
    repo.migrate()
    rep = evaluate_static_gate(settings_for(tmp_path), config, repo, now=START,
                               test_results=tmp_path / "missing.json")
    failed = {c.name for c in rep.failed}
    assert not rep.ok
    assert {"env_flags", "acknowledgement", "credentials", "automated_tests", "paper_validation"} <= failed


def _seed_paper_history(repo: Repository, days: int, trades: int, pnl_each: float) -> None:
    repo.add_equity_snapshot(START - timedelta(days=days), Mode.PAPER, 500, 500, 0, 0, 0, 0)
    for i in range(trades):
        repo.upsert_trade_fill(strategy_id="crypto_grid", venue="crypto", symbol="BTC/USDT", mode=Mode.PAPER,
                               quantity=0.001, entry_price=100, exit_price=101, entry_time=START, exit_time=START,
                               fees_usd=0.01, realized_pnl=pnl_each, exit_reason="tp",
                               exit_client_order_id=f"c{i}", is_virtual=True)


def _good_results(tmp_path: Path) -> Path:
    p = tmp_path / "res.json"
    p.write_text(json.dumps({"passed": 100, "failed": 0, "errors": 0, "full_suite": True,
                             "ts": START.isoformat(), "code_hash": source_hash()}))
    return p


def test_live_gate_passes_only_when_everything_ok(tmp_path, config):
    repo = Repository(tmp_path / "g.db")
    repo.migrate()
    _seed_paper_history(repo, 31, 100, 0.05)
    st = settings_for(tmp_path, live_trading=True, paper_trading=False,
                      live_trading_acknowledgement=LIVE_ACKNOWLEDGEMENT_PHRASE, crypto_api_key="k" * 10,
                      crypto_api_secret="s" * 10, alpaca_api_key_id="a" * 10, alpaca_api_secret_key="b" * 10,
                      alpaca_paper_account=False)
    rep = evaluate_static_gate(st, config, repo, now=START, test_results=_good_results(tmp_path))
    assert rep.ok, rep.text()
    # each single failure blocks live trading
    repo.set_kv("risk:live:state", {"drawdown_lock": True})
    assert not evaluate_static_gate(st, config, repo, now=START, test_results=_good_results(tmp_path)).ok
    repo.set_kv("risk:live:state", {})
    stale = evaluate_static_gate(st, config, repo, now=START + timedelta(days=8), test_results=_good_results(tmp_path))
    assert not stale.ok
    p = _good_results(tmp_path)
    data = json.loads(p.read_text())
    data["code_hash"] = "different"
    p.write_text(json.dumps(data))
    assert not evaluate_static_gate(st, config, repo, now=START, test_results=p).ok


def test_live_gate_rejects_insufficient_paper_history_or_losses(tmp_path, config):
    repo = Repository(tmp_path / "g.db")
    repo.migrate()
    _seed_paper_history(repo, 31, 100, -0.05)  # enough trades but net negative
    st = settings_for(tmp_path, live_trading=True, paper_trading=False,
                      live_trading_acknowledgement=LIVE_ACKNOWLEDGEMENT_PHRASE, crypto_api_key="k" * 10,
                      crypto_api_secret="s" * 10, alpaca_api_key_id="a" * 10, alpaca_api_secret_key="b" * 10,
                      alpaca_paper_account=False)
    rep = evaluate_static_gate(st, config, repo, now=START, test_results=_good_results(tmp_path))
    assert [c.name for c in rep.failed] == ["paper_validation"]


def test_main_never_starts_live_without_gate(monkeypatch, tmp_path):
    import trinity.main as m

    captured = {}

    async def fake_run(settings, config, mode, max_runtime=None):
        captured["mode"] = mode
        return 0

    monkeypatch.setattr(m, "run", fake_run)
    monkeypatch.setattr(m, "load_settings", lambda: settings_for(tmp_path, live_trading=True, paper_trading=False))
    assert m.main([]) == 0
    assert captured["mode"] == Mode.PAPER  # LIVE_TRADING=true alone never enables live after a (re)start


def test_run_live_refuses_without_interactive_console(monkeypatch, tmp_path, capsys):
    import trinity.scripts.run_live as rl

    monkeypatch.setattr(rl, "load_settings", lambda: settings_for(tmp_path))
    monkeypatch.setattr("sys.argv", ["run_live.py"])
    assert rl.main() == 1
    assert "LIVE TRADING NOT ALLOWED" in capsys.readouterr().out
