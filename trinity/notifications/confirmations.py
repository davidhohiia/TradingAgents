"""Two-step confirmation for important Telegram actions (SPEC_4.0 A2).

Step 1 issues a one-time 6-digit code bound to the requesting user; step 2 (/confirm <code>)
executes the action. Codes expire after 120 s, are single-use, and the request is discarded
after 3 wrong attempts. /pause and /emergency_stop never need confirmation.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from trinity.core.clock import Clock

CONFIRM_TTL = timedelta(seconds=120)
MAX_ATTEMPTS = 3
CONFIRMABLE = ("resume", "stop")


@dataclass
class Pending:
    action: str
    user_id: int
    code: str
    expires_at: datetime
    attempts: int = 0


class ConfirmationManager:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self._pending: dict[int, Pending] = {}  # one open request per user

    def request(self, action: str, user_id: int) -> Pending:
        if action not in CONFIRMABLE:
            raise ValueError(f"{action} does not use confirmations")
        p = Pending(action, user_id, f"{secrets.randbelow(10**6):06d}", self.clock.now() + CONFIRM_TTL)
        self._pending[user_id] = p
        return p

    def cancel(self, user_id: int) -> bool:
        return self._pending.pop(user_id, None) is not None

    def confirm(self, code: str, user_id: int) -> tuple[str | None, str]:
        """Returns (action, message). action is None when the confirmation is refused."""
        p = self._pending.get(user_id)
        if p is None:
            return None, "Nothing to confirm (request /resume or /stop first)."
        if self.clock.now() > p.expires_at:
            self._pending.pop(user_id, None)
            return None, "Confirmation code expired. Start again."
        if code.strip() != p.code:
            p.attempts += 1
            if p.attempts >= MAX_ATTEMPTS:
                self._pending.pop(user_id, None)
                return None, "Wrong code 3 times: request discarded."
            return None, f"Wrong code ({MAX_ATTEMPTS - p.attempts} attempts left)."
        self._pending.pop(user_id, None)
        return p.action, "confirmed"
