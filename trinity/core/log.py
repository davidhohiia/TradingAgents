"""Structured (JSON) logging with secret redaction and log rotation."""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SECRET_PATTERNS = [
    # Telegram bot tokens: 123456789:AA...
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),
    # bot<token> in Telegram URLs
    re.compile(r"(api\.telegram\.org/bot)[^/\s]+"),
    # key=value style secrets
    re.compile(
        r"(?i)((?:api[_-]?key|secret|password|passphrase|token|apca-api-[a-z-]+)"
        r"[\"']?\s*[:=]\s*[\"']?)([^\s\"',}]+)"
    ),
]

_registered_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """Register a literal secret value so it is masked in every log line."""
    if value and len(value) >= 6:
        _registered_secrets.add(value)


def redact(text: str) -> str:
    for secret in _registered_secrets:
        if secret in text:
            text = text.replace(secret, "***REDACTED***")
    text = _SECRET_PATTERNS[0].sub("***REDACTED***", text)
    text = _SECRET_PATTERNS[1].sub(r"\1***REDACTED***", text)
    text = _SECRET_PATTERNS[2].sub(r"\1***REDACTED***", text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        record.msg = redact(msg)
        record.args = None
        extra = getattr(record, "ctx", None)
        if isinstance(extra, dict):
            record.ctx = json.loads(redact(json.dumps(extra, default=str)))
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        ctx = getattr(record, "ctx", None)
        if isinstance(ctx, dict):
            payload.update(ctx)
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, UTC).strftime("%Y-%m-%d %H:%M:%S")
        base = f"{ts}Z {record.levelname:<7} {record.name}: {record.getMessage()}"
        ctx = getattr(record, "ctx", None)
        if isinstance(ctx, dict) and ctx:
            base += " " + json.dumps(ctx, default=str, ensure_ascii=False)
        if record.exc_info:
            base += "\n" + redact(self.formatException(record.exc_info))
        return base


def setup_logging(log_dir: Path | None, level: str = "INFO", console: bool = True) -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)
    redactor = RedactingFilter()
    if console:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(ConsoleFormatter())
        ch.addFilter(redactor)
        root.addHandler(ch)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.TimedRotatingFileHandler(
            log_dir / "trinity.jsonl", when="midnight", backupCount=30, utc=True, encoding="utf-8"
        )
        fh.setFormatter(JsonFormatter())
        fh.addFilter(redactor)
        root.addHandler(fh)
        eh = logging.handlers.RotatingFileHandler(
            log_dir / "errors.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
        eh.setLevel(logging.ERROR)
        eh.setFormatter(JsonFormatter())
        eh.addFilter(redactor)
        root.addHandler(eh)
    # HTTP client libraries log full URLs (Telegram URLs contain the bot token).
    for noisy in ("httpx", "httpcore", "urllib3", "ccxt", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, level: int, msg: str, **ctx: Any) -> None:
    logger.log(level, msg, extra={"ctx": ctx})
