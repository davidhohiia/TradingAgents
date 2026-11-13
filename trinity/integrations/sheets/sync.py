"""Google Sheets synchronisation (secondary journal; SQLite remains the primary database).

Reliability model (transactional outbox):
* a closed trade is written to ``journal_trades`` and ``sheets_outbox`` in the SAME SQLite
  transaction as the fill that closed it - nothing is lost on restart or while offline;
* the syncer sends pending rows; before appending it reads the trade IDs already in the sheet,
  so a crash between "append" and "mark sent" can never create a duplicate row;
* API calls are retried with exponential backoff; every failure is stored in ``sync_errors``
  and the rows stay pending until the next successful run (automatic catch-up after an outage);
* statistic sheets are rebuilt from SQLite on every run (idempotent).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from trinity.core.clock import Clock, RealClock
from trinity.core.log import get_logger
from trinity.core.models import Mode
from trinity.database.repository import Repository
from trinity.integrations.sheets.client import SheetsClient, SheetsError
from trinity.integrations.sheets.journal import JOURNAL_TZ, TRADES_HEADER, trade_row
from trinity.integrations.sheets.stats import BOT_HEADER, DAILY_HEADER, bot_stats, daily_stats, portfolio_rows

log = get_logger(__name__)

TITLE = "TRINITY 3.0 — Trading Journal"
SHEETS = ["Trades", "Daily Statistics", "Bot Statistics", "Portfolio"]
ID_KEY = "sheets:spreadsheet_id"
STATUS_KEY = "sheets:status"
APPEND_CHUNK = 200


@dataclass
class SyncResult:
    ok: bool
    appended: int = 0
    already_present: int = 0
    pending_left: int = 0
    errors: list[str] = field(default_factory=list)


class SheetsSyncer:
    def __init__(self, repo: Repository, client: SheetsClient, mode: Mode, *, spreadsheet_id: str = "",
                 portfolio_fn: Callable[[], dict[str, Any]] | None = None,
                 allocations_fn: Callable[[], dict[str, float]] | None = None,
                 tz: str = JOURNAL_TZ, title: str = TITLE, clock: Clock | None = None) -> None:
        self.repo = repo
        self.client = client
        self.mode = mode
        self.configured_id = spreadsheet_id.strip()
        self.portfolio_fn = portfolio_fn
        self.allocations_fn = allocations_fn or (lambda: {})
        self.tz = tz
        self.title = title
        self.clock = clock or RealClock()
        self._prepared = False

    # ------------------------------------------------------------------ helpers
    @property
    def spreadsheet_id(self) -> str:
        return self.configured_id or (self.repo.get_kv(ID_KEY) or "")

    def _error(self, operation: str, exc: Exception) -> str:
        retryable = getattr(exc, "retryable", True)
        msg = f"{type(exc).__name__}: {exc}"
        self.repo.add_sync_error(self.clock.now(), "google_sheets", operation, msg, retryable)
        log.warning("Google Sheets %s failed: %s", operation, msg)
        return msg

    def _set_status(self, **kv: Any) -> None:
        st = self.repo.get_kv(STATUS_KEY) or {}
        st.update(kv)
        st["pending"] = self.repo.outbox_stats()["pending"]
        self.repo.set_kv(STATUS_KEY, st)

    def status(self) -> dict[str, Any]:
        st = dict(self.repo.get_kv(STATUS_KEY) or {})
        st.update(self.repo.outbox_stats())
        st["spreadsheet_id"] = self.spreadsheet_id or None
        return st

    # ------------------------------------------------------------------ setup
    async def ensure_spreadsheet(self) -> str:
        sid = self.spreadsheet_id
        if not sid:
            created = await self.client.create_spreadsheet(self.title, SHEETS, self.tz)
            sid = created["spreadsheetId"]
            self.repo.set_kv(ID_KEY, sid)
            self.repo.add_system_event("INFO", "sheets", "created", f"spreadsheet created: {sid}",
                                       {"url": created.get("spreadsheetUrl")}, ts=self.clock.now())
            log.info("created Google Spreadsheet '%s' id=%s", self.title, sid)
        meta = await self.client.get_metadata(sid)
        props = {s["properties"]["title"]: s["properties"] for s in meta.get("sheets", [])}
        missing = [t for t in SHEETS if t not in props]
        if missing:
            res = await self.client.batch_update(sid, [{"addSheet": {"properties": {"title": t}}} for t in missing])
            for reply in res.get("replies", []):
                p = reply.get("addSheet", {}).get("properties")
                if p:
                    props[p["title"]] = p
        header = await self.client.get_values(sid, "Trades", "A1:X1")
        if not header or header[0] != TRADES_HEADER:
            await self.client.update_values(sid, "Trades", "A1", [TRADES_HEADER])
        freeze = [{"updateSheetProperties": {"properties": {"sheetId": p["sheetId"],
                                                            "gridProperties": {"frozenRowCount": 1}},
                                             "fields": "gridProperties.frozenRowCount"}}
                  for t, p in props.items() if t in SHEETS and "sheetId" in p]
        if freeze:
            await self.client.batch_update(sid, freeze)
        self._prepared = True
        return sid

    async def check_connection(self) -> tuple[bool, str]:
        try:
            sid = await self.ensure_spreadsheet()
            meta = await self.client.get_metadata(sid)
            title = meta.get("properties", {}).get("title", "?")
            self._set_status(connected=True, last_check=self.clock.now().isoformat(), title=title)
            return True, f"connected to '{title}' ({sid})"
        except (SheetsError, KeyError) as exc:
            msg = self._error("check_connection", exc)
            self._set_status(connected=False, last_check=self.clock.now().isoformat(), last_error=msg)
            return False, msg

    # ------------------------------------------------------------------ trades
    async def sync_trades(self) -> SyncResult:
        pending = self.repo.pending_outbox("trade")
        res = SyncResult(True)
        if not pending:
            return res
        sid = self.spreadsheet_id
        try:
            if not self._prepared or not sid:
                sid = await self.ensure_spreadsheet()
            existing = {row[0] for row in await self.client.get_values(sid, "Trades", "A2:A") if row}
            send, present = [], []
            for r in pending:
                (present if r["record_key"] in existing else send).append(r)
            if present:
                self.repo.mark_outbox_sent([r["id"] for r in present], self.clock.now())
                res.already_present = len(present)
            for i in range(0, len(send), APPEND_CHUNK):
                chunk = send[i:i + APPEND_CHUNK]
                rows = [trade_row(json.loads(r["payload_json"]), self.tz) for r in chunk]
                await self.client.append_rows(sid, "Trades", rows)
                self.repo.mark_outbox_sent([r["id"] for r in chunk], self.clock.now())
                res.appended += len(chunk)
        except SheetsError as exc:
            msg = self._error("sync_trades", exc)
            left = [r["id"] for r in self.repo.pending_outbox("trade")]
            self.repo.mark_outbox_failed(left, msg)
            res.ok = False
            res.errors.append(msg)
        res.pending_left = self.repo.outbox_stats()["pending"]
        return res

    # ------------------------------------------------------------------ statistics
    def _equity_points(self) -> list[tuple[datetime, float]]:
        return [(datetime.fromisoformat(r["ts"]), float(r["equity"])) for r in self.repo.equity_history(self.mode)]

    async def _rewrite(self, sid: str, sheet: str, rows: list[list[Any]]) -> None:
        await self.client.clear(sid, sheet)
        await self.client.update_values(sid, sheet, "A1", rows)

    async def sync_stats(self) -> SyncResult:
        res = SyncResult(True)
        trades = self.repo.list_journal_trades(self.mode)
        try:
            sid = self.spreadsheet_id if self._prepared else await self.ensure_spreadsheet()
            await self._rewrite(sid, "Daily Statistics",
                                [DAILY_HEADER, *daily_stats(trades, self._equity_points(), self.tz)])
            await self._rewrite(sid, "Bot Statistics", [BOT_HEADER, *bot_stats(trades, self.allocations_fn())])
            if self.portfolio_fn is not None:
                await self._rewrite(sid, "Portfolio", portfolio_rows(self.portfolio_fn(), self.tz))
        except SheetsError as exc:
            res.ok = False
            res.errors.append(self._error("sync_stats", exc))
        return res

    async def sync_all(self) -> SyncResult:
        t = await self.sync_trades()
        s = await self.sync_stats() if t.ok else SyncResult(False, errors=["stats skipped: trade sync failed"])
        ok = t.ok and s.ok
        now = self.clock.now().isoformat()
        if ok:
            self._set_status(connected=True, last_success=now, last_error=None)
        else:
            self._set_status(connected=False, last_failure=now, last_error=(t.errors + s.errors)[0])
        return SyncResult(ok, t.appended, t.already_present, t.pending_left, t.errors + s.errors)
