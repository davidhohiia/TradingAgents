"""SPEC_4.0 approved rules: A1 restart -> PAPER, A2 two-step confirmations, A3 emergency protocol,
A4 BALANCED profile (immutable limits, Trade Quality Score >= 70)."""

from __future__ import annotations

import pathlib
import re
from datetime import timedelta

import pytest
from pydantic import ValidationError

from trinity.ai.quality_score import QualityInputs, grade_of, score_trade
from trinity.config.settings import AppConfig, RiskConfig
from trinity.core.models import AssetClass, Mode, OrderIntent, OrderStatus, OrderType, Side
from trinity.exchanges.base_exchange import ExchangeUnavailable
from trinity.risk.profiles import BALANCED
from trinity.tests.test_portfolio import OWNER, STRANGER, make_pm, telegram, update


async def book(system, symbol="BTC/USDT"):
    return await system.crypto_market.fetch_order_book(symbol)


async def buy(system, *, limit_frac=None, qty=0.0003, score=80, stop_frac=0.97, symbol="BTC/USDT"):
    b = await book(system, symbol)
    price = round(b.best_bid * limit_frac, 2) if limit_frac else None
    return await system.engine.submit(OrderIntent(
        strategy_id="crypto_momentum", venue="crypto", symbol=symbol, side=Side.BUY,
        order_type=OrderType.LIMIT if price else OrderType.MARKET, quantity=qty, reference_price=b.best_ask,
        price=price, stop_price=(price or b.best_ask) * stop_frac, market_data_ts=b.ts,
        asset_class=AssetClass.CRYPTO, quality_score=score))


async def resting_tp(system, qty=0.0001):
    b = await book(system)
    return await system.engine.submit(OrderIntent(
        strategy_id="crypto_momentum", venue="crypto", symbol="BTC/USDT", side=Side.SELL, order_type=OrderType.LIMIT,
        quantity=qty, reference_price=b.best_bid, price=round(b.best_ask * 1.02, 2), market_data_ts=b.ts,
        asset_class=AssetClass.CRYPTO, reason="take profit"))


# =============================================================== A4: BALANCED profile
def test_balanced_profile_values_are_the_approved_ones(config):
    assert config.profile == "BALANCED"
    r = config.risk
    assert (r.max_risk_per_trade_pct, r.max_total_open_risk_pct, r.max_daily_loss_pct, r.max_weekly_loss_pct,
            r.drawdown_warning_pct, r.drawdown_reduce_pct, r.drawdown_live_lock_pct, r.drawdown_emergency_pct) == \
        (0.0075, 0.03, 0.03, 0.06, 0.10, 0.12, 0.15, 0.20)
    assert config.quality.min_score == BALANCED.min_trade_quality_score == 70


def test_config_deviating_from_profile_does_not_load(config):
    data = config.model_dump()
    for field, bad in (("max_daily_loss_pct", 0.04), ("max_risk_per_trade_pct", 0.005), ("drawdown_live_lock_pct", 0.18)):
        d = {**data, "risk": {**data["risk"], field: bad}}
        with pytest.raises(ValidationError, match="does not match risk profile"):
            AppConfig.model_validate(d)
    with pytest.raises(ValidationError):  # threshold may not go below 70
        AppConfig.model_validate({**data, "quality": {"min_score": 70, "per_bot": {"crypto_momentum": 65}}})
    with pytest.raises(ValidationError):
        AppConfig.model_validate({**data, "quality": {"min_score": 60, "per_bot": {}}})
    with pytest.raises(ValidationError, match="unknown risk profile"):
        AppConfig.model_validate({**data, "profile": "AGGRESSIVE"})
    stricter = AppConfig.model_validate({**data, "quality": {"min_score": 70, "per_bot": {"stock_trend": 80}}})
    assert stricter.quality.threshold("stock_trend") == 80 and stricter.quality.threshold("crypto_momentum") == 70


def test_limits_are_immutable_at_runtime(config):
    with pytest.raises(ValidationError):
        config.risk.max_risk_per_trade_pct = 0.02
    with pytest.raises(ValidationError):
        config.quality.min_score = 50


async def test_runtime_tampering_blocks_new_positions(system):
    system.config.risk = RiskConfig(max_daily_loss_pct=0.05)  # someone swaps the limits object
    res = await buy(system)
    assert not res.accepted and any("integrity" in r for r in res.reasons)
    assert any(r["event_type"] == "PROFILE_INTEGRITY" for r in system.repo.list_risk_events(20))


def test_ai_modules_have_no_code_path_to_change_limits():
    """Static guard: AI modules never assign to risk/quality/profile settings."""
    pattern = re.compile(r"(\.risk\.|\.quality\.|\.rc\.|profile)\w*\s*=(?!=)|setattr\(|object\.__setattr__")
    for path in pathlib.Path(__file__).resolve().parents[1].joinpath("ai").glob("*.py"):
        offending = [ln for ln in path.read_text().splitlines() if pattern.search(ln)]
        assert not offending, f"{path.name}: {offending}"


# =============================================================== Trade Quality Score
def strong_inputs(**kw):
    base = dict(adx=34, ema_aligned=True, ema_slope_pct=0.01, rsi=60, macd_hist=0.5, macd_rising=True,
                volume_ratio=1.6, atr_pct=0.012, spread_bps=2, depth_ratio=10, regime="bull", reward_risk=3,
                held_positions=0)
    base.update(kw)
    return QualityInputs(**base)


def test_quality_score_scale_and_missing_data():
    s = score_trade(strong_inputs())
    assert s.score == 100 and s.grade == "very strong" and not s.missing
    weak = score_trade(strong_inputs(adx=12, ema_aligned=False, ema_slope_pct=-0.01, rsi=80, macd_hist=-1,
                                     macd_rising=False, volume_ratio=0.5, regime="strong_bear", reward_risk=1.0))
    assert weak.score < 40 and weak.grade == "weak"
    partial = score_trade(strong_inputs(adx=None, depth_ratio=None))
    assert {"trend", "depth"} <= set(partial.missing) and partial.score == 100 - 15 - 4  # missing -> 0, not guessed
    corr = score_trade(strong_inputs(held_positions=1, max_correlation=0.9))
    assert corr.components["correlation"] == 0
    assert [grade_of(x) for x in (10, 45, 65, 80, 95)] == ["weak", "low", "acceptable", "strong", "very strong"]


async def test_risk_engine_enforces_quality_threshold(system):
    assert not (await buy(system, score=69)).accepted
    r_none = await buy(system, score=None)
    assert not r_none.accepted and "without Trade Quality Score" in r_none.reasons[0]
    assert (await buy(system, score=70)).accepted
    o = system.repo.list_orders(Mode.PAPER, limit=5)[0]
    assert o.quality_score == 70  # stored with the order
    b = await book(system)  # exits never need a score
    res = await system.engine.submit(OrderIntent(
        strategy_id="crypto_momentum", venue="crypto", symbol="BTC/USDT", side=Side.SELL, order_type=OrderType.MARKET,
        quantity=0.0003, reference_price=b.best_bid, market_data_ts=b.ts, asset_class=AssetClass.CRYPTO))
    assert res.accepted


async def test_quality_score_does_not_override_risk_limits(system):
    res = await buy(system, score=100, qty=0.002, stop_frac=0.5)  # perfect score, oversized risk
    assert not res.accepted and any("trade risk" in r or "max order" in r for r in res.reasons)


async def test_bot_records_every_signal_with_score_and_decision(tmp_path, config):
    from trinity.tests.conftest import build_system
    from trinity.tests.test_crypto_momentum import make_bot

    s = build_system(tmp_path, config, seed=21)
    for spec in s.crypto_market.specs.values():
        spec.annual_drift, spec.annual_vol = 15.0, 0.35
    s.crypto_market.__init__(s.crypto_market.name, s.crypto_market.asset_class, list(s.crypto_market.specs.values()),
                             s.clock, seed=21)
    bot = make_bot(s, config)
    await bot.start()
    for _ in range(48 * 6):
        await bot.safe_step()
        s.clock.advance(600)
    sigs = [dict(r) for r in s.repo.list_signals(Mode.PAPER, 1000)]
    assert sigs, "signals must be journaled"
    assert all(0 <= x["score"] <= 100 and x["threshold"] == 70 for x in sigs)
    for x in sigs:
        if x["decision"] == "submitted":
            assert x["score"] >= 70 and x["client_order_id"]
        if x["decision"] == "rejected_quality":
            assert x["score"] < 70
    for o in s.repo.list_orders(Mode.PAPER, limit=1000):
        if o.side == Side.BUY:
            assert o.quality_score is not None and o.quality_score >= 70


# =============================================================== A3: emergency protocol
async def _setup_positions(system, config):
    pm, notifier = make_pm(system, config)
    await pm.start()
    assert (await buy(system)).accepted  # filled market entry with stop at -3%
    entry = await buy(system, limit_frac=0.97, symbol="ETH/USDT", qty=0.005)  # resting entry order
    tp = await resting_tp(system)  # resting exit (take profit)
    assert entry.accepted and tp.accepted
    return pm, notifier, entry.client_order_id, tp.client_order_id


async def test_emergency_protocol_keeps_positions_under_control(system, config):
    pm, notifier, entry_cid, tp_cid = await _setup_positions(system, config)
    report = await pm.emergency_stop("test")
    assert system.repo.get_order(entry_cid).status == OrderStatus.CANCELED  # unfilled entry canceled
    assert system.repo.get_order(tp_cid).status.is_active  # exit order kept (only reduces risk)
    assert report["canceled_entries"] == 1 and len(report["positions"]) == 1
    assert not (await buy(system, symbol="SOL/USDT", qty=0.1)).accepted  # no new positions
    msg = [t for e, t, _ in notifier.sent if e == "emergency_stop"][0]
    assert "NOT liquidated" in msg and "BTC/USDT" in msg and "stop" in msg
    # bots keep running in protection-only mode
    steps_before = pm.bots["crypto_momentum"].steps
    await pm.tick()
    system.clock.advance(60)
    await pm.tick()
    assert pm.bots["crypto_momentum"].steps > steps_before
    # market crash below the stop -> guardian closes the position although emergency is active
    pos = system.positions.get("crypto_momentum", "crypto", "BTC/USDT")
    system.crypto_market.set_price("BTC/USDT", pos.stop_price * 0.95)
    system.clock.advance(1)
    rep = await pm.guardian.run_once()
    assert "crypto_momentum:BTC/USDT" in rep.exits_submitted
    assert not system.positions.get("crypto_momentum", "crypto", "BTC/USDT").is_open
    assert system.repo.get_order(tp_cid).status == OrderStatus.CANCELED  # resting TP superseded by the stop exit
    assert "guardian stop" in system.repo.list_journal_trades()[0]["close_reason"]


async def test_emergency_assigns_stop_to_unprotected_position(system, config):
    pm, _ = make_pm(system, config)
    assert (await buy(system)).accepted
    system.positions.set_protection("crypto_momentum", "crypto", "BTC/USDT", None, None)
    report = await pm.emergency_stop("test")
    pos = system.positions.get("crypto_momentum", "crypto", "BTC/USDT")
    assert pos.stop_price is not None and report["stops_assigned"]
    assert pos.stop_price == pytest.approx(pos.last_price * (1 - config.emergency.default_stop_pct), rel=1e-6)


async def test_emergency_during_outage_retries_cancels_and_reports(system, config):
    pm, notifier, entry_cid, _ = await _setup_positions(system, config)
    system.crypto.inject_failure("cancel_order", ExchangeUnavailable("down"), count=2)
    real_ticker = system.crypto.fetch_ticker

    async def down(symbol):
        raise ExchangeUnavailable("network down")

    system.crypto.fetch_ticker = down
    report = await pm.emergency_stop("test")
    assert report["entries_left"] == 1 and report["unguarded"]  # honest report, nothing hidden
    assert any(e == "connection_lost" for e, _, _ in notifier.sent)
    system.crypto.fetch_ticker = real_ticker
    system.clock.advance(15)
    await pm.guardian.run_once()  # connection back: cancel retried, protection active again
    assert system.repo.get_order(entry_cid).status == OrderStatus.CANCELED
    assert any(e == "connection_restored" for e, _, _ in notifier.sent)


# =============================================================== A2: Telegram confirmations
async def test_pause_and_emergency_are_instant(system, config):
    pm, _ = make_pm(system, config)
    tg, _ = telegram(pm)
    r1 = await tg.handle_update(update(OWNER, "/pause"))
    assert "/confirm" not in r1 and "paused" in system.risk.blocking_locks()
    r2 = await tg.handle_update(update(OWNER, "/emergency_stop", 2))
    assert "/confirm" not in r2 and "emergency_stop" in system.risk.blocking_locks()


async def test_stop_requires_two_steps_and_valid_code(system, config):
    pm, _ = make_pm(system, config)
    tg, _ = telegram(pm)
    assert (await buy(system, limit_frac=0.97)).accepted
    step1 = await tg.execute("/stop", OWNER)
    code = step1.split("/confirm ")[1].split()[0]
    assert system.risk.blocking_locks() == [] and len(system.repo.list_active_orders(Mode.PAPER)) == 1
    assert "No" in (await tg.execute(f"/confirm {code}", STRANGER)) or "Nothing" in (
        await tg.execute(f"/confirm {code}", STRANGER))  # bound to the requesting user
    assert "Wrong code" in await tg.execute("/confirm 000000" if code != "000000" else "/confirm 111111", OWNER)
    reply = await tg.execute(f"/confirm {code}", OWNER)
    assert reply.startswith("⏹") and "paused" in system.risk.blocking_locks()
    assert system.repo.list_active_orders(Mode.PAPER) == []  # entry canceled
    assert "Nothing to confirm" in await tg.execute(f"/confirm {code}", OWNER)  # single use


async def test_confirmation_code_expires_and_discards_after_three_wrong(system, config):
    pm, _ = make_pm(system, config)
    tg, _ = telegram(pm)
    code = (await tg.execute("/stop", OWNER)).split("/confirm ")[1].split()[0]
    system.clock.advance(121)
    assert "expired" in await tg.execute(f"/confirm {code}", OWNER)
    await tg.execute("/stop", OWNER)
    for _ in range(3):
        last = await tg.execute("/confirm 9999999", OWNER)
    assert "discarded" in last and system.risk.blocking_locks() == []
    await tg.execute("/stop", OWNER)
    assert "canceled" in await tg.execute("/cancel", OWNER)


async def test_unauthorized_user_cannot_confirm_anything(system, config):
    pm, _ = make_pm(system, config)
    tg, _ = telegram(pm)
    await tg.handle_update(update(OWNER, "/stop"))
    assert await tg.handle_update(update(STRANGER, "/confirm 123456", 2)) == "Access denied."


async def test_resume_runs_all_five_checks(system, config):
    pm, _ = make_pm(system, config)
    tg, _ = telegram(pm)
    assert (await buy(system)).accepted
    pm.pause("test")
    step1 = await tg.execute("/resume", OWNER)
    for name in ("connection:crypto", "positions", "risk_engine", "critical_errors", "balance_sync"):
        assert name in step1
    assert "/confirm" in step1


async def test_resume_refused_when_checks_fail(system, config):
    pm, _ = make_pm(system, config)
    tg, _ = telegram(pm)
    assert (await buy(system)).accepted
    pm.pause("test")
    system.positions.set_protection("crypto_momentum", "crypto", "BTC/USDT", None, None)  # unprotected position
    r = await tg.execute("/resume", OWNER)
    assert "Resume refused" in r and "no stop on" in r and "/confirm" not in r
    system.positions.set_protection("crypto_momentum", "crypto", "BTC/USDT", 50000, None)
    system.risk.on_equity(500)
    system.clock.advance(8 * 86400)
    system.risk.on_equity(420)  # 16% drawdown -> lock that /resume can never clear
    r = await tg.execute("/resume", OWNER)
    assert "Resume refused" in r and "drawdown_lock" in r


async def test_resume_checks_repeated_at_confirmation(system, config):
    pm, _ = make_pm(system, config)
    tg, _ = telegram(pm)
    pm.pause("test")
    code = (await tg.execute("/resume", OWNER)).split("/confirm ")[1].split()[0]

    async def down(symbol):
        raise ExchangeUnavailable("network down")

    system.crypto.fetch_ticker = down  # connection lost between step 1 and step 2
    r = await tg.execute(f"/confirm {code}", OWNER)
    assert "aborted" in r and "unreachable" in r
    assert "paused" in system.risk.blocking_locks()


# =============================================================== A1: restart -> PAPER, positions controlled
async def test_restart_with_active_positions_recovers_in_paper(tmp_path, config):
    from trinity.core.app import build_app, startup_reconcile
    from trinity.database.repository import Repository
    from trinity.tests.conftest import build_system
    from trinity.tests.test_recovery import settings_for

    db = tmp_path / "app.db"
    st = settings_for(tmp_path, live_trading=True, paper_trading=False)  # flags say LIVE...
    s = build_system(tmp_path, config)
    repo = Repository(db)
    repo.migrate()
    app = build_app(st, config, repo, Mode.PAPER, clock=s.clock, crypto_data=s.crypto_market,
                    stock_data=s.stock_market, arb_sources={})
    assert app.mode == Mode.PAPER
    b = await s.crypto_market.fetch_order_book("BTC/USDT")
    res = await app.engine.submit(OrderIntent(
        strategy_id="crypto_momentum", venue="crypto", symbol="BTC/USDT", side=Side.BUY, order_type=OrderType.MARKET,
        quantity=0.0003, reference_price=b.best_ask, stop_price=b.best_ask * 0.97, market_data_ts=b.ts,
        asset_class=AssetClass.CRYPTO, quality_score=85))
    assert res.accepted
    stop = app.engine.positions.get("crypto_momentum", "crypto", "BTC/USDT").stop_price
    # ---- crash: no shutdown, the process simply disappears
    del app
    app2 = build_app(st, config, Repository(db), Mode.PAPER, clock=s.clock, crypto_data=s.crypto_market,
                     stock_data=s.stock_market, arb_sources={})
    assert app2.mode == Mode.PAPER and all(v.is_paper for v in app2.venues.values())
    reports = await startup_reconcile(app2)
    assert all(r.ok for r in reports)
    pos = app2.engine.positions.get("crypto_momentum", "crypto", "BTC/USDT")
    assert pos.is_open and pos.stop_price == pytest.approx(stop)  # state restored from SQLite
    await app2.pm.start()
    assert "position_guardian" in app2.pm.scheduler.tasks
    s.crypto_market.set_price("BTC/USDT", stop * 0.97)  # market drops after the restart
    s.clock.advance(1)
    await app2.pm.guardian.run_once()
    assert not app2.engine.positions.get("crypto_momentum", "crypto", "BTC/USDT").is_open


async def test_restart_keeps_emergency_and_blocks_entries(tmp_path, config):
    from trinity.tests.conftest import build_system

    s = build_system(tmp_path, config)
    pm, _ = make_pm(s, config)
    assert (await buy(s)).accepted
    await pm.emergency_stop("before restart")
    s2 = build_system(tmp_path, config, db_path=tmp_path / "test.db", clock=s.clock)
    s2.crypto.load_state(s.crypto.to_state())
    pm2, _ = make_pm(s2, config)
    assert "emergency_stop" in s2.risk.blocking_locks()
    assert not (await buy(s2, symbol="ETH/USDT", qty=0.005)).accepted
    pos = s2.positions.get("crypto_momentum", "crypto", "BTC/USDT")
    s2.crypto_market.set_price("BTC/USDT", pos.stop_price * 0.95)
    s2.clock.advance(1)
    assert (await pm2.guardian.run_once()).exits_submitted  # still protected after the restart


def test_main_always_starts_paper_even_with_live_flags(monkeypatch, tmp_path):
    import trinity.main as m
    from trinity.tests.test_recovery import settings_for

    seen = {}

    async def fake_run(settings, config, mode, max_runtime=None):
        seen["mode"] = mode
        return 0

    monkeypatch.setattr(m, "run", fake_run)
    monkeypatch.setattr(m, "load_settings", lambda: settings_for(
        tmp_path, live_trading=True, paper_trading=False,
        live_trading_acknowledgement="I UNDERSTAND THAT LIVE TRADING CAN LOSE REAL MONEY"))
    for _ in range(2):  # every restart
        assert m.main([]) == 0
        assert seen["mode"] == Mode.PAPER
    import trinity.config.settings as cs

    assert not hasattr(cs.Settings(), "live_autoresume")  # no auto-resume setting exists


def test_live_start_requires_two_step_confirmation(monkeypatch, tmp_path, capsys):
    import trinity.scripts.run_live as rl
    from trinity.core.live_guard import LiveGateReport
    from trinity.tests.test_recovery import settings_for

    ok = LiveGateReport()
    ok.add("all", True, "ok")
    monkeypatch.setattr(rl, "load_settings", lambda: settings_for(tmp_path))
    monkeypatch.setattr(rl, "evaluate_static_gate", lambda *a, **k: ok)

    async def venues_ok(*a, **k):
        return ok

    monkeypatch.setattr(rl, "verify_venues", venues_ok)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.argv", ["run_live.py"])
    started = []
    import trinity.main as m

    monkeypatch.setattr(m, "main", lambda argv, _live_authorized=False: started.append(_live_authorized) or 0)

    answers = iter(["START LIVE TRADING", "000000x"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert rl.main() == 1 and started == []  # wrong code -> stays PAPER
    answers = iter(["start live trading"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert rl.main() == 1 and started == []  # wrong phrase

    def correct(prompt=""):
        if "Step 1/2" in prompt:
            return "START LIVE TRADING"
        return re.search(r"code (\d{6})", prompt).group(1)

    monkeypatch.setattr("builtins.input", correct)
    assert rl.main() == 0 and started == [True]
    assert "LIVE SUMMARY" in capsys.readouterr().out


# =============================================================== connection loss
async def test_connection_loss_reported_once_and_resume_blocked(system, config):
    pm, notifier = make_pm(system, config)
    tg, _ = telegram(pm)
    assert (await buy(system)).accepted
    pm.pause("test")
    system.crypto_market.outages = [(system.clock.now(), system.clock.now() + timedelta(minutes=30))]
    for _ in range(3):
        rep = await pm.guardian.run_once()
        system.clock.advance(15)
    assert rep.unguarded
    assert len([e for e, _, _ in notifier.sent if e == "connection_lost"]) == 1  # no alert spam
    assert "Resume refused" in await tg.execute("/resume", OWNER)
    system.clock.advance(31 * 60)
    await pm.guardian.run_once()
    assert len([e for e, _, _ in notifier.sent if e == "connection_restored"]) == 1
