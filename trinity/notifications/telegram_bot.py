"""Telegram Control Center (Telegram Bot API over HTTPS long polling).

Endpoints used (official Bot API, https://core.telegram.org/bots/api):
    https://api.telegram.org/bot<token>/getUpdates
    https://api.telegram.org/bot<token>/sendMessage

Security:
* only user ids in TELEGRAM_ALLOWED_USER_IDS may run commands; everyone else is refused;
* API keys/secrets are never sent (all outgoing text passes through redact());
* /resume clears only the pause flag (never drawdown / emergency / live locks);
* /emergency_stop blocks new orders before anything else happens.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from trinity.core.log import get_logger, redact
from trinity.core.portfolio_manager import PortfolioManager
from trinity.notifications import message_templates as tpl
from trinity.core.clock import RealClock
from trinity.notifications.confirmations import ConfirmationManager
from trinity.notifications.notifier import Notifier

log = get_logger(__name__)
API = "https://api.telegram.org"
MAX_LEN = 4000


class TelegramControlCenter(Notifier):
    def __init__(self, token: str, allowed_ids: set[int], pm: PortfolioManager | None = None,
                 client: httpx.AsyncClient | None = None) -> None:
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN missing")
        self._token = token
        self.allowed = set(allowed_ids)
        self.pm = pm
        self.client = client or httpx.AsyncClient(timeout=40)
        self._offset: int | None = None
        self._stop = asyncio.Event()
        self.unauthorized_attempts = 0
        self.confirmations = ConfirmationManager(pm.clock if pm is not None else RealClock())

    def _url(self, method: str) -> str:
        return f"{API}/bot{self._token}/{method}"

    # ------------------------------------------------------------------ outgoing
    async def send(self, chat_id: int, text: str) -> None:
        text = redact(text)[:MAX_LEN]
        try:
            r = await self.client.post(self._url("sendMessage"), json={"chat_id": chat_id, "text": text})
            if r.status_code != 200:
                log.warning("telegram sendMessage failed: HTTP %s", r.status_code)
        except httpx.HTTPError as exc:
            log.warning("telegram unreachable: %s", type(exc).__name__)

    async def notify(self, event_type: str, text: str, severity: str = "INFO") -> None:
        prefix = {"CRITICAL": "🚨 ", "WARNING": "⚠️ "}.get(severity, "")
        for uid in self.allowed:
            await self.send(uid, prefix + text)

    # ------------------------------------------------------------------ incoming
    def is_authorized(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.allowed

    async def handle_update(self, update: dict[str, Any]) -> str | None:
        msg = update.get("message") or update.get("edited_message")
        if not msg or "text" not in msg:
            return None
        user_id = (msg.get("from") or {}).get("id")
        chat_id = (msg.get("chat") or {}).get("id")
        text = msg["text"].strip()
        if not self.is_authorized(user_id):
            self.unauthorized_attempts += 1
            log.warning("unauthorized telegram command from user id %s", user_id)
            if self.pm is not None:
                self.pm.repo.add_system_event("WARNING", "telegram", "unauthorized",
                                              f"command refused for user {user_id}")
            reply = "Access denied."
        else:
            reply = await self.execute(text, user_id)
        if chat_id is not None:
            await self.send(chat_id, reply)
        return reply

    async def execute(self, text: str, user_id: int | None = None) -> str:
        cmd = text.split()[0].split("@")[0].lower() if text else ""
        uid = int(user_id) if user_id is not None else 0
        pm = self.pm
        if pm is None:
            return "System not running."
        try:
            if cmd in ("/start", "/help"):
                return f"{tpl.MODE_BANNER[pm.mode.value]}\n{tpl.HELP}"
            if cmd == "/status":
                return tpl.status(pm.status_data())
            if cmd == "/balance":
                return tpl.balance(pm.balance_data())
            if cmd == "/pnl":
                return tpl.pnl(pm.pnl_data())
            if cmd == "/positions":
                return tpl.positions(pm.positions_data(), pm.mode.value)
            if cmd == "/trades":
                rows = [dict(r) for r in pm.repo.list_trades(pm.mode, limit=10)]
                return tpl.trades(rows, pm.mode.value)
            if cmd in ("/crypto", "/grid", "/stocks", "/arbitrage"):
                sid = {"/crypto": "crypto_momentum", "/grid": "crypto_momentum", "/stocks": "stock_trend",
                       "/arbitrage": "arbitrage"}[cmd]
                bot = pm.bots.get(sid)
                return tpl.bot_detail(bot.describe()) if bot else f"{sid} not running."
            if cmd == "/capital":
                return tpl.capital(pm.capital_data())
            if cmd == "/strategies":
                return tpl.strategies(pm.strategies_data())
            if cmd == "/journal":
                st = pm.sheets.status() if pm.sheets else {**pm.repo.outbox_stats(), "configured": False}
                return ("Google Sheets journal: " + ("configured" if pm.sheets else "NOT configured (kept locally)")
                        + f"\npending {st.get('pending', 0)}, sent {st.get('sent', 0)}"
                        + (f"\nlast success {st.get('last_success')}" if st.get("last_success") else "")
                        + (f"\nlast error {st.get('last_error')}" if st.get("last_error") else ""))
            if cmd == "/risk":
                return tpl.risk(pm.risk_data())
            if cmd == "/report":
                return tpl.daily_report(pm.daily_report_data())
            if cmd == "/pause":  # instant, no confirmation
                pm.pause("telegram /pause")
                return "⏸ Paused: no new positions. Open positions stay managed (stops, take profits, exits)."
            if cmd == "/emergency_stop":  # instant, no confirmation
                r = await pm.emergency_stop("telegram /emergency_stop")
                return (f"🚨 Emergency protocol active. New positions blocked, {r['canceled_entries']} entry orders "
                        f"canceled{', ' + str(r['entries_left']) + ' pending retry' if r['entries_left'] else ''}. "
                        f"{len(r['positions'])} open positions kept under protective stops. "
                        "Clear locally: scripts/emergency_stop.py --clear")
            if cmd == "/resume":  # two-step
                checks = await pm.resume_checks()
                if not checks.ok:
                    return "▶️ Resume refused - checks failed:\n" + checks.text()
                pending = self.confirmations.request("resume", uid)
                return ("Resume checks passed:\n" + checks.text() + f"\n\nConfirm within 2 minutes: "
                        f"/confirm {pending.code}  (or /cancel). Checks are repeated on confirmation.")
            if cmd == "/stop":  # two-step
                pending = self.confirmations.request("stop", uid)
                return ("⏹ /stop will: block new positions, cancel unfilled entry orders, keep open positions under "
                        f"protective management. Confirm within 2 minutes: /confirm {pending.code}  (or /cancel)")
            if cmd == "/confirm":
                parts = text.split()
                action, msg = self.confirmations.confirm(parts[1] if len(parts) > 1 else "", uid)
                if action is None:
                    return msg
                if action == "resume":
                    rep = await pm.resume_confirmed()
                    return ("▶️ Resumed." if rep.ok else "Resume aborted - checks failed at confirmation:\n") + (
                        "" if rep.ok else rep.text())
                if action == "stop":
                    n = await pm.stop_trading()
                    return f"⏹ Stopped: new positions blocked, {n} entry orders canceled, positions stay managed."
            if cmd == "/cancel":
                return "Request canceled." if self.confirmations.cancel(uid) else "Nothing to cancel."
            if cmd == "/paper":
                v = pm.paper_validation()
                return ("🧪 Paper validation progress (minimum criteria, not a profit guarantee):\n"
                        f"days {v['days']}/{v['min_days']}, closed trades {v['closed_trades']}/{v['min_trades']}, "
                        f"net PnL ${v['net_pnl']:.2f}, critical execution errors {v['critical_execution_errors']}")
            if cmd == "/live_status":
                locks = pm.risk.blocking_locks()
                if pm.mode.value == "paper":
                    return ("LIVE trading is OFF. System runs in PAPER mode. Live can only be enabled locally "
                            "(LIVE_TRADING=true + checks + interactive confirmation). Telegram cannot enable it.")
                return f"🔴 LIVE mode. Locks: {', '.join(locks) or 'none'}"
            return f"Unknown command.\n{tpl.HELP}"
        except Exception as exc:  # noqa: BLE001
            log.exception("telegram command failed")
            return f"Command failed: {type(exc).__name__}"

    async def poll_once(self) -> int:
        params: dict[str, Any] = {"timeout": 25, "allowed_updates": '["message"]'}
        if self._offset is not None:
            params["offset"] = self._offset
        r = await self.client.get(self._url("getUpdates"), params=params, timeout=35)
        if r.status_code != 200:
            raise httpx.HTTPError(f"getUpdates HTTP {r.status_code}")
        data = r.json()
        updates = data.get("result", [])
        for u in updates:
            self._offset = int(u["update_id"]) + 1
            await self.handle_update(u)
        return len(updates)

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self.poll_once()
                backoff = 1.0
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("telegram polling error (%s), retry in %.0fs", type(exc).__name__, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)

    def stop(self) -> None:
        self._stop.set()

    async def close(self) -> None:
        self.stop()
        await self.client.aclose()
