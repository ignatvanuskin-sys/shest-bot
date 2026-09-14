"""Structured JSON logging with a traceable ``session_id`` for one lead pipeline."""
from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

# Correlates all log lines of a single lead extraction → dedup → sheet sync chain.
_session_id_var: ContextVar[str | None] = ContextVar("leadforge_session_id", default=None)

# Extra fields we surface on the JSON line when present in `extra={...}`.
_KNOWN_EXTRA_FIELDS = (
    "session_id",
    "model",
    "tokens_in",
    "tokens_out",
    "cost_usd_est",
    "latency_ms",
    "success",
    "action",
    "score",
    "reason",
    "telegram_user_id",
    "lead_id",
    "sheet_row",
)


def set_session_id(session_id: str | None) -> None:
    _session_id_var.set(session_id)


def get_session_id() -> str | None:
    return _session_id_var.get()


class JsonFormatter(logging.Formatter):
    """Emits one JSON object per log record."""

    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        session_id = _session_id_var.get()
        if session_id:
            data["session_id"] = session_id
        for key in _KNOWN_EXTRA_FIELDS:
            if hasattr(record, key):
                data[key] = getattr(record, key)
        if record.exc_info:
            data["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False, default=str)


def log_json(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    """Helper to log a message with structured extra fields."""
    logger.log(level, message, extra=fields)


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    root = logging.getLogger()
    root.setLevel(level)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.handlers = [handler]

    # Keep noisy third-party loggers out of the way.
    for name in ("aiogram", "aiohttp", "httpx", "gspread", "google.auth", "asyncio", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)
    return root
