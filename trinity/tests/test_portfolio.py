"""Portfolio manager, risk reactions, Telegram control center, supervision, reports."""

from __future__ import annotations

import httpx
import pytest

from trinity.bots.base import BaseBot
from trinity.bots.crypto_grid.bot import CryptoGridBot
from trinity.bots.stock_trend.bot import StockTrendBot
from trinity.core.log import redact, register_secret
from trinity.core.market_calendar import UsEquityCalendar
from trinity.core.models import AssetClass, Mode, OrderIntent, OrderStatus, OrderType, Side
from trinity.core.order_gateway import OrderGateway, StrategyMismatch
from trinity.core.portfolio_manager import MAX_BOT_RESTARTS, PortfolioManager
from trinity.notifications.notifier import LogNotifier
from trinity.notifications.telegram_bot import TelegramControlCenter

OWNER = 111222333
STRANGER = 999


def make_pm(system, config, extra_bots=None):
    notifier = LogNotifier()
    grid = CryptoGridBot(config.strategies.crypto_grid, gateway=OrderGateway(system.engine, "crypto_grid", "crypto"),
                         market=system.crypto_market, repo=system.repo, mode=Mode.PAPER, clock=system.clock,
                         notifier=notifier)
    stock = StockTrendBot(config.strategies.stock_trend, config, UsEquityCalendar(config.session),
                          gateway=OrderGateway(system.engine, "stock_trend", "stocks"), market=system.stocks,
                          repo=system.repo, mode=Mode.PAPER, clock=system.clock, notifier=notifier)
    pm = PortfolioManager(config=config, repo=system.repo, engine=system.engine, mode=Mode.PAPER,
                          bots=[grid, stock, *(extra_bots or [])], clock=system.clock, notifier=notifier)
    return pm, notifier


def telegram(pm, handler=None):
    sent = []

    def default(req: httpx.Request) -> httpx.Response:
        sent.append(req)
        return httpx.Response(200, json={"ok": True, "result": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler or default))
    return TelegramControlCenter("123456789:ABCdefGhIJKlmnOPQrstUVwxyz0123456789", {OWNER}, pm, client), sent


def update(user_id, text, uid=1):
    return {"update_id": uid, "message": {"from": {"id": user_id}, "chat": {"id": user_id}, "text": text}}


async def run_for(pm, system, seconds, step=30):
    for _ in range(int(seconds / step)):
        await pm.tick()
        system.clock.advance(step)


async def test_pm_runs_bots_and_tracks_equity(system, config):
    pm, _ = make_pm(system, config)
    await pm.start()
    await run_for(pm, system, 3600)
    assert system.repo.equity_history(Mode.PAPER)
    assert pm.last_equity is not None and pm.last_equity == pytest.approx(500, abs=10)
    health = {r["strategy_id"]: r["status"] for r in system.repo.list_bot_health()}
    assert health["crypto_grid"] == "PAPER MODE"
    bal = pm.balance_data()
    assert bal["reserve"] == 200 and set(bal["strategies"]) >= {"crypto_grid", "stock_trend", "arbitrage"}
    assert bal["strategies"]["arbitrage"]["available"] == 0


async def test_daily_loss_reaction_cancels_buys_and_notifies(system, config):
    pm, notifier = make_pm(system, config)
    await pm.start()
    await run_for(pm, system, 600)
    assert [o for o in system.repo.list_active_orders(Mode.PAPER) if o.side == Side.BUY]
    # simulate a realized loss of $12 today on the books
    p = system.positions.get("stock_trend", "stocks", "SPY")
    p.realized_pnl -= 12.0
    await pm.monitor()
    assert "daily_loss_lock" in system.risk.blocking_locks()
    assert not [o for o in system.repo.list_active_orders(Mode.PAPER) if o.side == Side.BUY]
    assert any(e == "risk_limit" for e, _, _ in notifier.sent)


async def test_drawdown_lock_cancels_all_and_keeps_positions(system, config):
    pm, notifier = make_pm(system, config)
    await pm.start()
    await run_for(pm, system, 1200)
    open_before = [p.symbol for p in system.positions.all(open_only=True)]
    p = system.positions.get("stock_trend", "stocks", "SPY")
    p.realized_pnl -= 45.0  # equity -9%
    await pm.monitor()
    assert "drawdown_lock" in system.risk.blocking_locks()
    assert system.repo.list_active_orders(Mode.PAPER) == []
    assert [p.symbol for p in system.positions.all(open_only=True)] == open_before  # no forced liquidation
    assert any("drawdown" in t.lower() for _, t, _ in notifier.sent)


async def test_telegram_rejects_strangers(system, config):
    pm, _ = make_pm(system, config)
    tg, sent = telegram(pm)
    reply = await tg.handle_update(update(STRANGER, "/emergency_stop"))
    assert reply == "Access denied." and tg.unauthorized_attempts == 1
    assert not system.risk.blocking_locks()
    reply = await tg.handle_update(update(None, "/status"))
    assert reply == "Access denied."


async def test_telegram_commands_for_owner(system, config):
    pm, _ = make_pm(system, config)
    await pm.start()
    await run_for(pm, system, 300)
    tg, _ = telegram(pm)
    for cmd in ("/start", "/status", "/balance", "/pnl", "/positions", "/trades", "/grid", "/stocks", "/arbitrage",
                "/risk", "/report", "/paper", "/live_status"):
        reply = await tg.execute(cmd)
        assert reply and "Command failed" not in reply, (cmd, reply)
    assert "PAPER" in await tg.execute("/status")
    assert "OFF" in await tg.execute("/live_status")


async def test_telegram_emergency_stop_and_resume_cannot_bypass(system, config):
    pm, _ = make_pm(system, config)
    await pm.start()
    await run_for(pm, system, 300)
    tg, _ = telegram(pm)
    reply = await tg.handle_update(update(OWNER, "/emergency_stop"))
    assert "Emergency stop" in reply
    assert "emergency_stop" in system.risk.blocking_locks()
    assert system.repo.list_active_orders(Mode.PAPER) == []
    b = await system.crypto_market.fetch_order_book("BTC/USDT")
    res = await system.engine.submit(OrderIntent(
        strategy_id="crypto_grid", venue="crypto", symbol="BTC/USDT", side=Side.BUY, order_type=OrderType.MARKET,
        quantity=0.0002, reference_price=b.best_ask, market_data_ts=b.ts, asset_class=AssetClass.CRYPTO,
        stop_price=b.best_ask * 0.97))
    assert not res.accepted
    reply = await tg.handle_update(update(OWNER, "/resume", 2))
    assert "remains blocked" in reply and "emergency_stop" in reply
    assert "emergency_stop" in system.risk.blocking_locks()


async def test_telegram_pause_resume(system, config):
    pm, _ = make_pm(system, config)
    tg, _ = telegram(pm)
    await tg.execute("/pause")
    assert system.risk.blocking_locks() == ["paused"]
    assert (await tg.execute("/resume")).startswith("▶️")
    assert system.risk.blocking_locks() == []


async def test_telegram_never_leaks_secrets(system, config):
    register_secret("SUPERSECRETAPIKEY123")
    pm, _ = make_pm(system, config)
    sent_bodies = []

    def handler(req):
        sent_bodies.append(req.content.decode())
        return httpx.Response(200, json={"ok": True})

    tg, _ = telegram(pm, handler)
    await tg.notify("x", "key=SUPERSECRETAPIKEY123 token 123456789:ABCdefGhIJKlmnOPQrstUVwxyz0123456789")
    assert sent_bodies and all("SUPERSECRETAPIKEY123" not in b and "ABCdefGhIJ" not in b for b in sent_bodies)
    assert "SUPERSECRET" not in redact("api_key: SUPERSECRETAPIKEY123")


async def test_telegram_poll_parses_updates(system, config):
    pm, _ = make_pm(system, config)
    calls = []

    def handler(req: httpx.Request):
        calls.append(req)
        if "getUpdates" in str(req.url):
            return httpx.Response(200, json={"ok": True, "result": [update(OWNER, "/status", 41)]})
        return httpx.Response(200, json={"ok": True})

    tg, _ = telegram(pm, handler)
    assert await tg.poll_once() == 1
    assert tg._offset == 42
    assert any("sendMessage" in str(c.url) for c in calls)


class ExplodingBot(BaseBot):
    bot_type = "test"

    async def step(self) -> None:
        raise RuntimeError("boom")


async def test_supervisor_restarts_then_stops_failing_bot(system, config):
    bad = ExplodingBot(strategy_id="exploder", gateway=None, market=system.crypto_market, repo=system.repo,
                       mode=Mode.PAPER, clock=system.clock, loop_interval=10)
    pm, notifier = make_pm(system, config, [bad])
    await pm.start()
    for _ in range(4000):
        await pm.tick()
        system.clock.advance(10)
        if not bad.running:
            break
    assert pm.restarts["exploder"] >= MAX_BOT_RESTARTS and not bad.running
    assert any(e == "bot_stopped" for e, _, _ in notifier.sent)
    assert pm.bots["crypto_grid"].running  # other bots unaffected


async def test_gateway_cannot_act_for_other_strategy(system):
    gw = OrderGateway(system.engine, "crypto_grid", "crypto")
    b = await system.crypto_market.fetch_order_book("BTC/USDT")
    with pytest.raises(StrategyMismatch):
        await gw.submit(OrderIntent(strategy_id="stock_trend", venue="crypto", symbol="BTC/USDT", side=Side.BUY,
                                    order_type=OrderType.MARKET, quantity=0.0002, reference_price=b.best_ask,
                                    market_data_ts=b.ts, asset_class=AssetClass.CRYPTO))
    other = OrderGateway(system.engine, "stock_trend", "stocks")
    res = await gw.submit(OrderIntent(strategy_id="crypto_grid", venue="crypto", symbol="BTC/USDT", side=Side.BUY,
                                      order_type=OrderType.MARKET, quantity=0.0002, reference_price=b.best_ask,
                                      market_data_ts=b.ts, asset_class=AssetClass.CRYPTO, stop_price=b.best_ask * 0.97))
    assert not await other.cancel(res.client_order_id)


async def test_daily_report_generated_once_per_day(system, config):
    pm, notifier = make_pm(system, config)
    await pm.start()
    await run_for(pm, system, 26 * 3600, step=300)
    reports = [t for e, t, _ in notifier.sent if e == "daily_report"]
    assert len(reports) == 1 and "Daily report" in reports[0] and "not guarantee" in reports[0]


async def test_paper_validation_progress(system, config):
    pm, _ = make_pm(system, config)
    v = pm.paper_validation()
    assert v["min_days"] == 30 and v["min_trades"] == 100 and not v["days_ok"]


async def test_stop_command_cancels_entries_keeps_positions(system, config):
    pm, _ = make_pm(system, config)
    await pm.start()
    await run_for(pm, system, 900)
    positions = len(system.positions.all(open_only=True))
    await pm.stop_trading()
    assert not [o for o in system.repo.list_active_orders(Mode.PAPER) if o.side == Side.BUY]
    assert len(system.positions.all(open_only=True)) == positions
    assert all(o.status != OrderStatus.PENDING_SUBMIT for o in system.repo.list_orders(Mode.PAPER, 500))
