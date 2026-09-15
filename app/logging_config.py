"""Structured JSON logging with a traceable ``session_id`` for one lead pipeline."""
from __future__ import annotations

import json
import logging
import os
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Mapping

# Correlates all log lines of a single lead extraction → dedup → sheet sync chain.
_session_id_var: ContextVar[str | None] = ContextVar("leadforge_session_id", default=None)

# Persistent log file (ТЗ §11). The default path lives on the Railway volume; when
# the directory is unavailable (local dev, read-only fs) file logging is skipped
# and stdout keeps working — logging must never take the process down.
DEFAULT_LOG_FILE = "/data/logs/leadforge.log"
DEFAULT_LOG_MAX_BYTES = 5_000_000
DEFAULT_LOG_BACKUP_COUNT = 3
# LOG_FILE values that switch file logging off.
_LOG_FILE_OFF = frozenset({"", "0", "off", "none", "false", "no"})

# Attributes every ``LogRecord`` carries by itself. Everything *else* found in
# ``record.__dict__`` was passed through ``log_json(..., **fields)``/``extra=`` and
# must survive into the JSON line: a whitelist here silently dropped diagnostics
# the code does pass (chat_id, error_type, callback_query_id, update_id,
# update_type, callback_data, ...).
_RESERVED_RECORD_FIELDS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
) | {"message", "asctime", "taskName"}


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
        session_id = get_session_id()
        if session_id:
            data["session_id"] = session_id
        # ``extra`` fields (and an explicit session_id passed with them) win.
        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_FIELDS:
                continue
            data[key] = value
        if record.exc_info:
            data["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False, default=str)


def log_json(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    """Helper to log a message with structured extra fields."""
    logger.log(level, message, extra=fields)


def _int_env(env: Mapping[str, str], name: str, default: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _coerce_log_file(value: str | Path | None) -> Path | None:
    """Normalise a LOG_FILE value; returns None when file logging is off."""
    if value is None:
        return None
    raw = str(value).strip()
    return None if raw.lower() in _LOG_FILE_OFF else Path(raw)


def resolve_log_file(env: Mapping[str, str] | None = None) -> Path | None:
    """Path of the rotating log file, or None when file logging is off."""
    env = os.environ if env is None else env
    return _coerce_log_file(env.get("LOG_FILE", DEFAULT_LOG_FILE))


def build_file_handler(
    path: str | Path, max_bytes: int = DEFAULT_LOG_MAX_BYTES, backup_count: int = DEFAULT_LOG_BACKUP_COUNT
) -> RotatingFileHandler | None:
    """Rotating file handler for *path*, or None when the path is not usable."""
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            target, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
    except OSError:
        return None
    handler.setFormatter(JsonFormatter())
    return handler


def setup_logging(
    level: int = logging.INFO,
    *,
    log_file: str | Path | None = None,
    max_bytes: int | None = None,
    backup_count: int | None = None,
) -> logging.Logger:
    """Configure stdout (always) + a rotating file log (when the path is usable).

    ``LOG_FILE`` / ``LOG_MAX_BYTES`` / ``LOG_BACKUP_COUNT`` configure the file sink;
    the explicit keyword arguments override them (used by tests).
    """
    root = logging.getLogger()
    root.setLevel(level)

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(JsonFormatter())
    handlers: list[logging.Handler] = [stdout_handler]

    env = os.environ
    path = resolve_log_file(env) if log_file is None else _coerce_log_file(log_file)
    size = _int_env(env, "LOG_MAX_BYTES", DEFAULT_LOG_MAX_BYTES) if max_bytes is None else max_bytes
    backups = (
        _int_env(env, "LOG_BACKUP_COUNT", DEFAULT_LOG_BACKUP_COUNT)
        if backup_count is None
        else backup_count
    )

    file_handler = build_file_handler(path, size, backups) if path is not None else None
    if file_handler is not None:
        handlers.append(file_handler)

    root.handlers = handlers

    if path is not None and file_handler is None:
        # A missing/read-only log directory (local dev without the /data volume)
        # must not break startup — stdout stays the log sink.
        logging.getLogger(__name__).warning(
            "file logging disabled: %s is not writable (stdout only)", path
        )
    elif file_handler is not None:
        logging.getLogger(__name__).info(
            "file logging enabled: %s (max %s bytes, %s backups)", path, size, backups
        )

    # Keep noisy third-party loggers out of the way.
    for name in ("aiogram", "aiohttp", "httpx", "gspread", "google.auth", "asyncio", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)
    return root
