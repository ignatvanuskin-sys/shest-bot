"""FIX-5 regression: logs must not lose diagnostics, and they must survive a restart.

Two problems:

1. ``JsonFormatter`` copied only a hard-coded whitelist of ``extra`` fields, so
   ``chat_id``, ``error_type``, ``callback_query_id``, ``update_id``, ``update_type``
   and ``callback_data`` — all of them passed by ``app.bot.safe`` /
   ``app.bot.errors`` — never appeared in the JSON line.
2. the logs went to stdout only: nothing was left on the volume after a redeploy
   (ТЗ §11 asks for a rotating file log).
"""
from __future__ import annotations

import json
import logging
import logging.handlers
from pathlib import Path

import pytest
from aiogram.types import Update

from app.bot.errors import update_log_context
from app.logging_config import (
    DEFAULT_LOG_BACKUP_COUNT,
    DEFAULT_LOG_MAX_BYTES,
    JsonFormatter,
    get_session_id,
    log_json,
    resolve_log_file,
    set_session_id,
    setup_logging,
)

TEST_BOT_TOKEN = "123456:TEST-TOKEN"
OWNER_USER_ID = 424242


def _record(**extra) -> logging.LogRecord:
    record = logging.LogRecord("app.test", logging.ERROR, __file__, 1, "boom", (), None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def _payload(record: logging.LogRecord) -> dict:
    return json.loads(JsonFormatter().format(record))


def _remove_file_handlers() -> None:
    for handler in list(logging.getLogger().handlers):
        if isinstance(handler, logging.handlers.RotatingFileHandler):
            handler.close()


@pytest.fixture
def restore_root_logging():
    """``setup_logging`` replaces ``root.handlers`` wholesale — put them back."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    try:
        yield root
    finally:
        for handler in list(root.handlers):
            if handler not in saved_handlers:
                handler.close()
        root.handlers = saved_handlers
        root.setLevel(saved_level)
        set_session_id(None)


@pytest.fixture
def capture_records():
    """Attach a collector to the app loggers (caplog is detached by setup_logging)."""
    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Collector(level=logging.DEBUG)
    targets = [
        logging.getLogger(name)
        for name in (
            "app.bot.safe",
            "app.bot.flow",
            "app.services.lead_service",
            "app.services.dedup",
        )
    ]
    for target in targets:
        target.addHandler(handler)
        target.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        for target in targets:
            target.removeHandler(handler)


# ---------------- (1) no field is dropped ----------------
def test_fields_passed_to_log_json_all_survive():
    payload = _payload(
        _record(
            action="callback_stale",
            error_type="aiogram.exceptions.TelegramBadRequest",
            reason="query is too old",
            chat_id=424242,
            telegram_user_id=424242,
            callback_query_id="callback-17",
            update_id=900001,
            update_type="callback_query",
            callback_data="add",
            lead_id=3,
            sheet_row=7,
            some_future_field={"nested": [1, 2]},
        )
    )

    for key, expected in {
        "action": "callback_stale",
        "error_type": "aiogram.exceptions.TelegramBadRequest",
        "chat_id": 424242,
        "telegram_user_id": 424242,
        "callback_query_id": "callback-17",
        "update_id": 900001,
        "update_type": "callback_query",
        "callback_data": "add",
        "lead_id": 3,
        "sheet_row": 7,
        "some_future_field": {"nested": [1, 2]},
    }.items():
        assert payload[key] == expected, f"{key} was dropped from the JSON line"

    # The standard record attributes are still not dumped as "extra" fields.
    assert "levelname" not in payload and "pathname" not in payload
    assert payload["level"] == "ERROR" and payload["logger"] == "app.test"


def test_update_error_context_from_errors_module_survives():
    """What ``app.bot.errors`` passes must be readable in production logs."""
    update = Update.model_validate(
        {
            "update_id": 900005,
            "callback_query": {
                "id": "callback-5",
                "chat_instance": "ci",
                "from": {"id": OWNER_USER_ID, "is_bot": False, "first_name": "T"},
                "data": "add",
                "message": {
                    "message_id": 1,
                    "date": 1_700_000_000,
                    "chat": {"id": OWNER_USER_ID, "type": "private"},
                    "from": {"id": OWNER_USER_ID, "is_bot": False, "first_name": "T"},
                    "text": "card",
                },
            },
        }
    )
    context = update_log_context(update)
    payload = _payload(_record(**context, action="update_error", error_type="builtins.TypeError"))

    assert payload["update_id"] == 900005
    assert payload["update_type"] == "callback_query"
    assert payload["callback_data"] == "add"
    assert payload["chat_id"] == OWNER_USER_ID
    assert payload["telegram_user_id"] == OWNER_USER_ID
    assert payload["error_type"] == "builtins.TypeError"


def test_safe_send_fields_survive_through_a_real_log_call(capture_records, restore_root_logging):
    from app.bot.safe import _log_send_failure
    from aiogram.exceptions import TelegramBadRequest

    exc = TelegramBadRequest(method=None, message="Bad Request: chat not found")
    _log_send_failure(
        "send failed",
        exc,
        action="collecting_prompt",
        chat_id=OWNER_USER_ID,
        user_id=OWNER_USER_ID,
        callback_query_id="callback-9",
    )

    record = capture_records[-1]
    payload = _payload(record)
    assert payload["chat_id"] == OWNER_USER_ID
    assert payload["callback_query_id"] == "callback-9"
    assert payload["error_type"] == "aiogram.exceptions.TelegramBadRequest"
    assert payload["action"] == "collecting_prompt"
    assert "exc_info" in payload, "the traceback must still be rendered"


def test_context_session_id_is_used_when_the_record_carries_none():
    set_session_id("42")
    try:
        assert _payload(_record(action="x"))["session_id"] == "42"
    finally:
        set_session_id(None)
    assert "session_id" not in _payload(_record(action="x"))


def test_an_explicit_session_id_on_the_record_wins():
    set_session_id("42")
    try:
        assert _payload(_record(session_id="7"))["session_id"] == "7"
    finally:
        set_session_id(None)
    assert get_session_id() is None


def test_non_serialisable_extra_does_not_break_the_log_line():
    payload = _payload(_record(action="x", weird=object()))
    assert payload["action"] == "x"
    assert isinstance(payload["weird"], str)


# ---------------- (2) the rotating file log ----------------
def test_log_file_defaults_to_the_volume_path(monkeypatch):
    monkeypatch.delenv("LOG_FILE", raising=False)
    assert resolve_log_file() == Path("/data/logs/leadforge.log")


def test_log_file_can_be_switched_off(monkeypatch):
    for value in ("off", "OFF", "0", "none", "false", ""):
        monkeypatch.setenv("LOG_FILE", value)
        assert resolve_log_file() is None, f"LOG_FILE={value!r} must disable file logging"


def test_setup_logging_writes_a_rotating_json_file(tmp_path, monkeypatch, restore_root_logging):
    log_path = tmp_path / "logs" / "leadforge.log"
    monkeypatch.setenv("LOG_FILE", str(log_path))
    monkeypatch.setenv("LOG_MAX_BYTES", "2048")
    monkeypatch.setenv("LOG_BACKUP_COUNT", "2")

    setup_logging(logging.INFO)

    handlers = logging.getLogger().handlers
    rotating = [h for h in handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(rotating) == 1
    assert rotating[0].maxBytes == 2048
    assert rotating[0].backupCount == 2
    assert isinstance(rotating[0].formatter, JsonFormatter)
    # stdout stays a sink next to the file.
    assert any(type(h) is logging.StreamHandler for h in handlers)

    log_json(logging.getLogger("app.test"), logging.INFO, "hello", action="test", chat_id=5)
    rotating[0].flush()

    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    payload = json.loads(lines[-1])
    assert payload["message"] == "hello"
    assert payload["action"] == "test"
    assert payload["chat_id"] == 5


def test_setup_logging_uses_the_documented_defaults(tmp_path, monkeypatch, restore_root_logging):
    monkeypatch.delenv("LOG_MAX_BYTES", raising=False)
    monkeypatch.delenv("LOG_BACKUP_COUNT", raising=False)
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "leadforge.log"))

    setup_logging(logging.INFO)

    rotating = [
        h
        for h in logging.getLogger().handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert rotating[0].maxBytes == DEFAULT_LOG_MAX_BYTES
    assert rotating[0].backupCount == DEFAULT_LOG_BACKUP_COUNT


def test_setup_logging_accepts_explicit_overrides(tmp_path, restore_root_logging):
    setup_logging(logging.INFO, log_file=tmp_path / "explicit.log", max_bytes=1024, backup_count=1)

    rotating = [
        h
        for h in logging.getLogger().handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert rotating[0].maxBytes == 1024 and rotating[0].backupCount == 1


def test_setup_logging_off_keeps_only_stdout(monkeypatch, restore_root_logging):
    monkeypatch.setenv("LOG_FILE", "off")

    setup_logging(logging.INFO)

    assert not [
        h
        for h in logging.getLogger().handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]


def test_unusable_log_directory_does_not_break_startup(tmp_path, monkeypatch, restore_root_logging):
    """A read-only / missing volume path must degrade to stdout, never raise."""
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    monkeypatch.setenv("LOG_FILE", str(blocker / "nested" / "leadforge.log"))

    setup_logging(logging.INFO)  # must not raise

    handlers = logging.getLogger().handlers
    assert not [h for h in handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert any(type(h) is logging.StreamHandler for h in handlers)

    # Logging still works (stdout only).
    log_json(logging.getLogger("app.test"), logging.INFO, "still alive", action="test")
