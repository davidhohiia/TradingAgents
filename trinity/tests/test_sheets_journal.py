"""Google Sheets trading journal: P&L maths, fill merging, time zone, outbox sync (mocked Sheets API)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import unquote

import httpx
import pytest

from trinity.core.models import AssetClass, ExchangeOrder, Mode, OrderIntent, OrderStatus, OrderType, Side
from trinity.database.repository import Repository
from trinity.integrations.sheets.client import SheetsClient
from trinity.integrations.sheets.journal import (
    TRADES_HEADER,
    TradeCycle,
    close_cycle,
    compute_trade_pnl,
    local_date_time,
    trade_row,
)
from trinity.integrations.sheets.stats import bot_stats, daily_stats, portfolio_rows
from trinity.integrations.sheets.sync import SHEETS, TITLE, SheetsSyncer


# ---------------------------------------------------------------- P&L maths (hand-checked examples)
def test_long_pnl_example():
    # buy 0.5 @ 100, sell 0.5 @ 110, fees 0.05 + 0.055
    p = compute_trade_pnl("LONG", 100, 110, 0.5, 0.105)
    assert p.gross == pytest.approx(5.0)
    assert p.net == pytest.approx(4.895)
    assert p.capital_employed == pytest.approx(50.0)
    assert p.net_pct == pytest.approx(9.79)


def test_short_pnl_example():
    # short 2 @ 50, cover @ 45, fees 0.2, funding 0.1
    p = compute_trade_pnl("SHORT", 50, 45, 2, 0.2, funding=0.1)
    assert p.gross == pytest.approx(10.0)
    assert p.net == pytest.approx(9.7)
    assert p.net_pct == pytest.approx(9.7)
    losing = compute_trade_pnl("SHORT", 50, 55, 2, 0.2)
    assert losing.gross == pytest.approx(-10.0) and losing.net == pytest.approx(-10.2)


def test_leveraged_return_uses_margin_and_invalid_inputs():
    p = compute_trade_pnl("LONG", 100, 102, 1, 0.0, leverage=2.0)
    assert p.capital_employed == pytest.approx(50) and p.net_pct == pytest.approx(4.0)
    with pytest.raises(ValueError):
        compute_trade_pnl("LONG", 100, 102, 0, 0)
    with pytest.raises(ValueError):
        compute_trade_pnl("SIDEWAYS", 100, 102, 1, 0)


def test_cycle_merges_partial_fills_into_one_trade():
    c = TradeCycle(opened_at="2026-03-02T09:00:00+00:00", first_entry_order="b1")
    c.add_entry(0.2, 100.0, 0.02, "b1")
    c.add_entry(0.3, 101.0, 0.0303, "b1")  # partial fills of the same order
    c.add_entry(0.5, 99.0, 0.0495, "b2")
    t1 = datetime(2026, 3, 2, 15, tzinfo=UTC)
    c.add_exit(0.6, 105.0, 0.063, "s1", "take profit", t1)
    c.add_exit(0.4, 104.0, 0.0416, "s2", "trailing stop", t1)
    t = close_cycle(c, mode="paper", strategy_id="crypto_momentum", venue="crypto", symbol="ETH/USDT")
    entry_vwap = (0.2 * 100 + 0.3 * 101 + 0.5 * 99) / 1.0
    exit_vwap = (0.6 * 105 + 0.4 * 104) / 1.0
    assert t.entry_price == pytest.approx(entry_vwap) and t.exit_price == pytest.approx(exit_vwap)
    assert t.quantity == pytest.approx(1.0)
    assert t.fees == pytest.approx(0.02 + 0.0303 + 0.0495 + 0.063 + 0.0416)
    assert t.gross_pnl == pytest.approx(exit_vwap - entry_vwap)
    assert t.net_pnl == pytest.approx(t.gross_pnl - t.fees)
    assert (t.entry_fills, t.exit_fills) == (3, 2)
    assert t.close_reason == "take profit; trailing stop"
    assert t.trade_id.startswith("P-ETHUSDT-20260302090000-")


def test_amsterdam_time_zone_including_dst():
    assert local_date_time("2026-01-15T23:30:00+00:00") == ("2026-01-16", "00:30:00")  # CET = UTC+1
    assert local_date_time("2026-07-15T23:30:00+00:00") == ("2026-07-16", "01:30:00")  # CEST = UTC+2


def test_trade_row_layout_and_virtual_label():
    c = TradeCycle(opened_at="2026-03-02T09:00:00+00:00", first_entry_order="b1")
    c.add_entry(1, 100, 0.1, "b1")
    c.add_exit(1, 101, 0.1, "s1", "stop", datetime(2026, 3, 2, 10, tzinfo=UTC))
    t = close_cycle(c, mode="paper", strategy_id="crypto_momentum", venue="crypto", symbol="BTC/USDT")
    row = trade_row(t)
    assert len(row) == len(TRADES_HEADER) and TRADES_HEADER[16] == "Причина закрытия"
    assert row[1] == "Crypto Momentum AI" and row[3] == "LONG"
    assert row[6:10] == ["2026-03-02", "10:00:00", "2026-03-02", "11:00:00"]
    assert row[11] == pytest.approx(0.2) and row[12] == pytest.approx(0.8) and row[17] == pytest.approx(1.0)
    assert row[15] == "CLOSED (VIRTUAL)"
    live = close_cycle(c, mode="live", strategy_id="crypto_momentum", venue="crypto", symbol="BTC/USDT")
    assert trade_row(live)[15] == "CLOSED" and live.trade_id != t.trade_id


# ---------------------------------------------------------------- journal from real engine fills
async def _book(system):
    return await system.crypto_market.fetch_order_book("BTC/USDT")


async def test_engine_partial_fills_produce_exactly_one_journal_trade(system):
    b = await _book(system)
    price = round(b.best_bid * 0.99, 2)
    res = await system.engine.submit(OrderIntent(
        strategy_id="crypto_momentum", venue="crypto", symbol="BTC/USDT", side=Side.BUY, order_type=OrderType.LIMIT,
        quantity=0.0004, reference_price=b.best_ask, price=price, stop_price=price * 0.98, market_data_ts=b.ts,
        asset_class=AssetClass.CRYPTO, sub_strategy="momentum"))
    system.crypto_market.set_price("BTC/USDT", price * (1 - 0.00012))  # touch -> partial fill
    system.clock.advance(1)
    await system.engine.sync_orders()
    system.crypto_market.set_price("BTC/USDT", price * 0.99)  # through -> rest fills
    system.clock.advance(1)
    await system.engine.sync_orders()
    assert system.repo.get_order(res.client_order_id).status == OrderStatus.FILLED
    assert len(system.repo.list_executions()) == 2
    assert system.repo.list_journal_trades() == []  # still open: nothing journaled
    for qty, reason in ((0.0001, "partial take profit"), (0.0003, "stop loss")):
        b = await _book(system)
        await system.engine.submit(OrderIntent(
            strategy_id="crypto_momentum", venue="crypto", symbol="BTC/USDT", side=Side.SELL,
            order_type=OrderType.MARKET, quantity=qty, reference_price=b.best_bid, market_data_ts=b.ts,
            asset_class=AssetClass.CRYPTO, reason=reason))
    journal = system.repo.list_journal_trades(Mode.PAPER)
    assert len(journal) == 1
    t = journal[0]
    execs = [dict(r) for r in system.repo.query("SELECT * FROM executions ORDER BY id")]
    buys = [e for e in execs if e["side"] == "buy"]
    sells = [e for e in execs if e["side"] == "sell"]
    assert t["entry_price"] == pytest.approx(sum(e["quantity"] * e["price"] for e in buys) / 0.0004)
    assert t["exit_price"] == pytest.approx(sum(e["quantity"] * e["price"] for e in sells) / 0.0004)
    assert t["fees"] == pytest.approx(sum(e["fee_usd"] for e in execs))  # exact recorded fees
    realized = sum(r["realized_pnl"] for r in system.repo.query("SELECT realized_pnl FROM trades"))
    assert t["net_pnl"] == pytest.approx(realized)  # consistent with the accounting engine
    assert t["gross_pnl"] == pytest.approx(t["net_pnl"] + t["fees"])
    assert t["sub_strategy"] == "momentum" and "stop loss" in t["close_reason"]
    assert (t["entry_fills"], t["exit_fills"]) == (2, 2)
    assert system.repo.outbox_stats()["pending"] == 1


async def test_fee_in_base_dust_closes_cycle(system):
    b = await _book(system)
    res = await system.engine.submit(OrderIntent(
        strategy_id="crypto_momentum", venue="crypto", symbol="BTC/USDT", side=Side.BUY, order_type=OrderType.LIMIT,
        quantity=0.0003, reference_price=b.best_ask, price=round(b.best_bid * 0.99, 2),
        stop_price=b.best_bid * 0.95, market_data_ts=b.ts, asset_class=AssetClass.CRYPTO))
    rec = system.repo.get_order(res.client_order_id)
    await system.engine._apply_update(rec, ExchangeOrder(
        rec.client_order_id, rec.exchange_order_id, "BTC/USDT", Side.BUY, OrderType.LIMIT, OrderStatus.FILLED,
        rec.quantity, rec.quantity, rec.price, rec.price, fee=0.0000003, fee_currency="BTC"))
    pos = system.positions.get("crypto_momentum", "crypto", "BTC/USDT")
    assert pos.quantity == pytest.approx(0.0002997)  # fee taken in BTC
    b = await _book(system)
    system.crypto.balances["BTC"].free = pos.quantity
    await system.engine.submit(OrderIntent(  # the venue lot step (0.00001) only allows selling 0.00029
        strategy_id="crypto_momentum", venue="crypto", symbol="BTC/USDT", side=Side.SELL, order_type=OrderType.MARKET,
        quantity=pos.quantity, reference_price=b.best_bid, market_data_ts=b.ts, asset_class=AssetClass.CRYPTO,
        reason="exit"))
    remaining = system.positions.get("crypto_momentum", "crypto", "BTC/USDT").quantity
    assert 0 < remaining < 0.00001  # unsellable dust left on the account
    journal = system.repo.list_journal_trades()
    assert len(journal) == 1 and journal[0]["quantity"] == pytest.approx(0.00029)


def test_journal_insert_is_idempotent(system):
    c = TradeCycle(opened_at="2026-03-02T09:00:00+00:00", first_entry_order="b1")
    c.add_entry(1, 100, 0.1, "b1")
    c.add_exit(1, 101, 0.1, "s1", "x", datetime(2026, 3, 2, 10, tzinfo=UTC))
    t = close_cycle(c, mode="paper", strategy_id="crypto_momentum", venue="crypto", symbol="BTC/USDT").to_dict()
    assert system.repo.insert_journal_trade(t, system.clock.now())
    assert not system.repo.insert_journal_trade(t, system.clock.now())
    assert len(system.repo.list_journal_trades()) == 1 and system.repo.outbox_stats()["pending"] == 1


def test_funding_is_part_of_the_trade_cost(system):
    from trinity.core.models import Fill

    pm = system.positions
    ts = system.clock.now()
    pm.apply_fill(Fill("e1", "b1", "crypto_momentum", "crypto", "BTC/USDT", Mode.PAPER, Side.BUY, 0.001, 60000,
                       0.06, ts), leverage=1.5)
    pm.apply_funding("crypto_momentum", "crypto", "BTC/USDT", 0.03, ts)
    pm.apply_fill(Fill("e2", "s1", "crypto_momentum", "crypto", "BTC/USDT", Mode.PAPER, Side.SELL, 0.001, 61000,
                       0.061, ts), exit_reason="tp")
    t = system.repo.list_journal_trades()[0]
    assert t["gross_pnl"] == pytest.approx(1.0) and t["funding"] == pytest.approx(0.03)
    assert t["net_pnl"] == pytest.approx(1.0 - 0.121 - 0.03)
    assert t["capital_employed"] == pytest.approx(60 / 1.5)


# ---------------------------------------------------------------- statistics
def _jt(tid, sid, sub, net, exit_time, fees=0.1):
    return {"trade_id": tid, "strategy_id": sid, "sub_strategy": sub, "net_pnl": net, "exit_time": exit_time,
            "fees": fees}


def test_daily_and_bot_statistics():
    trades = [_jt("a", "crypto_momentum", "momentum", 3.0, "2026-03-02T10:00:00+00:00"),
              _jt("b", "crypto_momentum", "breakout", -1.0, "2026-03-02T22:30:00+00:00"),  # 23:30 Amsterdam
              _jt("c", "stock_trend", "trend", 2.0, "2026-03-02T23:30:00+00:00")]  # 00:30 next day Amsterdam
    eq = [(datetime(2026, 3, 1, 22, tzinfo=UTC), 500.0), (datetime(2026, 3, 2, 22, tzinfo=UTC), 502.0),
          (datetime(2026, 3, 3, 10, tzinfo=UTC), 504.0)]
    rows = daily_stats(trades, eq)
    assert rows[0] == ["2026-03-02", 2, 3.0, -1.0, 2.0, pytest.approx(0.4), 502.0]
    assert rows[1][0] == "2026-03-03" and rows[1][4] == 2.0 and rows[1][6] == 504.0
    assert rows[1][5] == pytest.approx(2.0 / 502.0 * 100)
    bots = bot_stats(trades, {"crypto_momentum": 150.0, "stock_trend": 100.0, "arbitrage": 0.0})
    cm = next(r for r in bots if r[0] == "Crypto Momentum AI")
    assert cm[1:5] == [2, 1, 1, 50.0] and cm[9] == pytest.approx(2.0) and cm[10] == pytest.approx(2 / 150 * 100)
    assert any(r[0] == "Crypto Momentum AI / breakout" and r[9] == -1.0 for r in bots)


def test_daily_balance_never_invented():
    rows = daily_stats([_jt("a", "crypto_momentum", "momentum", 1.0, "2026-03-02T10:00:00+00:00")], [])
    assert rows[0][5] == "" and rows[0][6] == ""  # no recorded equity -> blank, not estimated


def test_portfolio_rows():
    rows = portfolio_rows({"mode": "paper", "ts": "2026-03-02T12:00:00+00:00", "contributed": 700.0,
                           "initial_deposit": 500.0, "additional_deposits": 200.0, "equity": 714.0, "cash": 600.0,
                           "in_positions": 114.0, "drawdown_pct": 0.012, "reserve": 450.0})
    d = dict(rows)
    assert d["Начальный депозит (USD)"] == 500 and d["Дополнительные пополнения (USD)"] == 200
    assert d["Общая прибыль/убыток (USD, без пополнений)"] == pytest.approx(14)
    assert d["Доходность (%)"] == pytest.approx(2.0) and d["Текущая просадка (%)"] == pytest.approx(1.2)
    assert d["Обновлено (Europe/Amsterdam)"] == "2026-03-02 13:00:00"


# ---------------------------------------------------------------- mocked Google Sheets API
class FakeSheets:
    """In-memory imitation of the Sheets v4 REST endpoints used by TRINITY."""

    def __init__(self) -> None:
        self.books: dict[str, dict[str, list[list]]] = {}
        self.titles: dict[str, str] = {}
        self.offline = False
        self.fail_queue: list[int] = []
        self.fail_after_append = False
        self.calls: list[str] = []
        self.tokens_seen: list[str] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(f"{req.method} {req.url.path}")
        self.tokens_seen.append(req.headers.get("Authorization", ""))
        if self.offline:
            raise httpx.ConnectError("offline", request=req)
        if self.fail_queue:
            return httpx.Response(self.fail_queue.pop(0), json={"error": {"message": "simulated"}})
        path = unquote(req.url.path)
        if path == "/v4/spreadsheets" and req.method == "POST":
            body = json.loads(req.content)
            sid = f"sheet{len(self.books) + 1}"
            self.books[sid] = {s["properties"]["title"]: [] for s in body["sheets"]}
            self.titles[sid] = body["properties"]["title"]
            return httpx.Response(200, json={"spreadsheetId": sid, "spreadsheetUrl": f"https://x/{sid}"})
        sid = path.split("/")[3].split(":")[0]
        if sid not in self.books:
            return httpx.Response(404, json={"error": {"message": "Requested entity was not found."}})
        book = self.books[sid]
        if path.endswith(":batchUpdate"):
            replies = []
            for r in json.loads(req.content)["requests"]:
                if "addSheet" in r:
                    t = r["addSheet"]["properties"]["title"]
                    book[t] = []
                    replies.append({"addSheet": {"properties": {"title": t, "sheetId": len(book)}}})
                else:
                    replies.append({})
            return httpx.Response(200, json={"replies": replies})
        if "/values/" not in path:
            return httpx.Response(200, json={"spreadsheetId": sid, "properties": {"title": self.titles[sid]},
                                             "sheets": [{"properties": {"title": t, "sheetId": i}}
                                                        for i, t in enumerate(book)]})
        rng = path.split("/values/")[1]
        sheet = rng.split("!")[0].split(":")[0].strip("'")
        cells = rng.split("!")[1] if "!" in rng else ""
        rows = book[sheet]
        if rng.endswith(":append"):
            rows.extend(json.loads(req.content)["values"])
            if self.fail_after_append:
                self.fail_after_append = False
                return httpx.Response(503, json={"error": {"message": "lost response"}})
            return httpx.Response(200, json={})
        if rng.endswith(":clear"):
            book[sheet] = []
            return httpx.Response(200, json={})
        if req.method == "PUT":
            vals = json.loads(req.content)["values"]
            if cells.startswith("A1"):
                book[sheet][: len(vals)] = vals
            return httpx.Response(200, json={})
        if cells.startswith("A2"):
            return httpx.Response(200, json={"values": [[r[0]] for r in rows[1:]]})
        if cells.startswith("A1"):
            return httpx.Response(200, json={"values": rows[:1]})
        return httpx.Response(200, json={"values": rows})


def make_syncer(repo, fake, spreadsheet_id="", portfolio=None, attempts=3):
    refresh_calls = []

    async def token(force=False):
        refresh_calls.append(force)
        return "test-access-token"

    async def no_sleep(_):
        return None

    client = SheetsClient(token, transport=httpx.MockTransport(fake.handler), max_attempts=attempts, sleep=no_sleep)
    s = SheetsSyncer(repo, client, Mode.PAPER, spreadsheet_id=spreadsheet_id, portfolio_fn=portfolio,
                     allocations_fn=lambda: {"crypto_momentum": 150.0, "stock_trend": 100.0})
    s._refresh_calls = refresh_calls  # type: ignore[attr-defined]
    return s


def seed_trade(repo, n, mode="paper"):
    c = TradeCycle(opened_at=f"2026-03-02T09:{n:02d}:00+00:00", first_entry_order=f"b{n}")
    c.add_entry(0.001, 60000, 0.06, f"b{n}")
    c.add_exit(0.001, 60600, 0.0606, f"s{n}", "take profit", datetime(2026, 3, 2, 12, n, tzinfo=UTC))
    t = close_cycle(c, mode=mode, strategy_id="crypto_momentum", venue="crypto", symbol="BTC/USDT").to_dict()
    assert repo.insert_journal_trade(t, datetime.now(UTC))
    return t


def portfolio():
    return {"mode": "paper", "ts": "2026-03-02T12:00:00+00:00", "contributed": 500.0, "initial_deposit": 500.0,
            "additional_deposits": 0.0, "equity": 501.0, "cash": 501.0, "in_positions": 0.0, "drawdown_pct": 0.0,
            "reserve": 250.0}


async def test_creates_spreadsheet_with_all_sheets_and_headers(system):
    fake = FakeSheets()
    s = make_syncer(system.repo, fake, portfolio=portfolio)
    ok, msg = await s.check_connection()
    assert ok, msg
    sid = s.spreadsheet_id
    assert fake.titles[sid] == TITLE == "TRINITY 3.0 — Trading Journal"
    assert set(SHEETS) <= set(fake.books[sid]) and fake.books[sid]["Trades"][0] == TRADES_HEADER
    assert system.repo.get_kv("sheets:spreadsheet_id") == sid  # remembered across restarts
    assert all(t == "Bearer test-access-token" for t in fake.tokens_seen)


async def test_sync_appends_once_and_never_duplicates(system):
    fake = FakeSheets()
    s = make_syncer(system.repo, fake, portfolio=portfolio)
    trades = [seed_trade(system.repo, i) for i in range(3)]
    res = await s.sync_all()
    assert res.ok and res.appended == 3
    sheet = fake.books[s.spreadsheet_id]["Trades"]
    assert [r[0] for r in sheet[1:]] == [t["trade_id"] for t in trades]
    assert (await s.sync_all()).appended == 0  # nothing pending
    seed_trade(system.repo, 10)
    assert (await s.sync_all()).appended == 1
    assert len(fake.books[s.spreadsheet_id]["Trades"]) == 1 + 4
    stats = fake.books[s.spreadsheet_id]
    assert stats["Daily Statistics"][1][0] == "2026-03-02" and stats["Daily Statistics"][1][1] == 4
    assert stats["Bot Statistics"][1][0] == "Crypto Momentum AI"
    assert dict(stats["Portfolio"])["Текущий баланс (USD)"] == 501.0


async def test_offline_keeps_rows_locally_and_catches_up(system):
    fake = FakeSheets()
    s = make_syncer(system.repo, fake, portfolio=portfolio)
    await s.check_connection()
    fake.offline = True
    seed_trade(system.repo, 1)
    seed_trade(system.repo, 2)
    res = await s.sync_all()
    assert not res.ok and res.pending_left == 2
    errs = system.repo.list_sync_errors()
    assert errs and "network error" in errs[0]["error"]
    assert system.repo.query("SELECT attempts FROM sheets_outbox")[0][0] == 1
    # "restart": brand new repository + syncer on the same DB file -> nothing lost
    repo2 = Repository(system.repo.path)
    assert repo2.outbox_stats()["pending"] == 2
    fake.offline = False
    s2 = make_syncer(repo2, fake, portfolio=portfolio)
    res = await s2.sync_all()
    assert res.ok and res.appended == 2 and repo2.outbox_stats() == {"pending": 0, "sent": 2}


async def test_lost_append_response_does_not_duplicate(system):
    fake = FakeSheets()
    s = make_syncer(system.repo, fake, portfolio=portfolio, attempts=1)
    await s.check_connection()
    seed_trade(system.repo, 1)
    fake.fail_after_append = True  # rows written, but the client sees an error
    assert not (await s.sync_trades()).ok
    assert system.repo.outbox_stats()["pending"] == 1
    res = await s.sync_trades()
    assert res.ok and res.appended == 0 and res.already_present == 1
    assert len(fake.books[s.spreadsheet_id]["Trades"]) == 2  # header + exactly one row


async def test_retries_rate_limit_and_server_errors(system):
    fake = FakeSheets()
    s = make_syncer(system.repo, fake, portfolio=portfolio, attempts=4)
    await s.check_connection()
    seed_trade(system.repo, 1)
    fake.fail_queue = [429, 503]  # two transient failures, then success
    res = await s.sync_trades()
    assert res.ok and res.appended == 1


async def test_non_retryable_error_and_token_refresh(system):
    fake = FakeSheets()
    s = make_syncer(system.repo, fake, spreadsheet_id="does-not-exist")
    ok, msg = await s.check_connection()
    assert not ok and "404" in msg  # wrong id / no access: clear error, no endless retries
    fake2 = FakeSheets()
    s2 = make_syncer(system.repo, fake2, portfolio=portfolio)
    await s2.check_connection()
    fake2.fail_queue = [401]
    seed_trade(system.repo, 5)
    assert (await s2.sync_trades()).ok
    assert True in s2._refresh_calls  # expired token refreshed once


async def test_pm_task_and_telegram_journal_command(system, config):
    from trinity.tests.test_portfolio import make_pm, telegram

    pm, notifier = make_pm(system, config)
    fake = FakeSheets()
    pm.sheets = make_syncer(system.repo, fake, portfolio=pm.journal_portfolio_data)
    await pm.start()
    assert "sheets_sync" in pm.scheduler.tasks
    seed_trade(system.repo, 1)
    await pm.sync_sheets()
    assert system.repo.outbox_stats()["sent"] == 1
    tg, _ = telegram(pm)
    reply = await tg.execute("/journal")
    assert "configured" in reply and "sent 1" in reply
    fake.offline = True
    seed_trade(system.repo, 2)
    await pm.sync_sheets()
    await pm.sync_sheets()
    assert len([e for e, _, _ in notifier.sent if e == "sheets"]) == 1  # alert once, not spam


def test_factory_disabled_or_unconfigured(tmp_path, system):
    from trinity.integrations.sheets.factory import build_sheets_syncer
    from trinity.tests.test_portfolio import make_pm
    from trinity.tests.test_recovery import settings_for

    pm, _ = make_pm(system, system.config)
    assert build_sheets_syncer(settings_for(tmp_path), pm) is None
    st = settings_for(tmp_path, google_sheets_enabled=True, google_service_account_file=tmp_path / "missing.json")
    assert build_sheets_syncer(st, pm) is None  # missing key file -> journal stays local
