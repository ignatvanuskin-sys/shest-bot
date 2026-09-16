"""Regression tests for failure-tolerant Telegram I/O (``app.bot.safe``).

Production symptom these tests lock down: a single unsuccessful ``send_message``
(the owner blocked the bot, ``chat not found``, a button pressed on a card older
than the bot's last restart) escaped the handler as an exception, the webhook
answered 500 and Telegram re-delivered the *same* update forever.

The contract under test:

* a Telegram/transport failure on any outgoing call is logged at ERROR and the
  update still counts as handled (``UNHANDLED`` is never returned);
* a stale callback query turns into a fresh «Карточка устарела» message instead
  of a crash, and the handler keeps going;
* a real programming error (``TypeError``, bad kwarg) is *not* swallowed — it
  still reaches the global error handler in ``app.bot.errors`` and is logged.

Everything below runs through the real dispatcher (see
``tests.integration_harness``); the only fake I/O boundary is the recording bot,
and no test opens a socket.
"""
from __future__ import annotations

import json
import logging
import socket

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
)
from aiogram.methods import AnswerCallbackQuery, SendMessage

from app.bot.keyboards import CB_ADD, CB_DONE, CB_EDIT
from app.bot.middlewares import RetryMiddleware
from app.bot.safe import STALE_CARD_TEXT, is_stale_query_error, safe_answer_callback, safe_send
from app.bot.states import LeadForm
from app.schemas.extraction import ExtractionResult
from tests.integration_harness import (
    BASE_DATE,
    OWNER_USER_ID,
    STRANGER_USER_ID,
    assert_valid_telegram_html,
    harness,  # noqa: F401  — imported fixture
    no_network,  # noqa: F401  — autouse imported fixture (blocks non-loopback sockets)
    wait_until,
)

# ---------------- helpers ----------------

# Telegram's answer to a press on a card it no longer knows about.
STALE_QUERY_MESSAGE = (
    "Bad Request: query is too old and response timeout expired or query ID is invalid"
)

_CALLBACK_METHOD = AnswerCallbackQuery(callback_query_id="stale")


def stale_query_error() -> TelegramBadRequest:
    return TelegramBadRequest(method=_CALLBACK_METHOD, message=STALE_QUERY_MESSAGE)


def forbidden_error() -> TelegramForbiddenError:
    return TelegramForbiddenError(
        method=SendMessage(chat_id=OWNER_USER_ID, text="x"),
        message="Forbidden: bot was blocked by the user",
    )


def chat_not_found_error() -> TelegramBadRequest:
    return TelegramBadRequest(
        method=SendMessage(chat_id=OWNER_USER_ID, text="x"),
        message="Bad Request: chat not found",
    )


def network_error() -> TelegramNetworkError:
    return TelegramNetworkError(
        method=SendMessage(chat_id=OWNER_USER_ID, text="x"),
        message="Connection reset by peer",
    )


def break_method(monkeypatch, obj, name: str, exc: BaseException) -> None:
    """Replace ``obj.<name>`` with an async stub that raises *exc*."""

    async def failing(*args, **kwargs):
        raise exc

    monkeypatch.setattr(obj, name, failing)


def errors(caplog) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.levelno >= logging.ERROR]


def actions(caplog) -> set[str | None]:
    return {getattr(record, "action", None) for record in errors(caplog)}


def full_result() -> ExtractionResult:
    return ExtractionResult(
        company_name="Ромашка",
        city="Алматы",
        phone_raw="+7 700 123 45 67",
        phone_e164="+77001234567",
        instagram="romashka",
        source_guess="2gis",
    )


def test_no_network_guard_is_active():
    """Meta-test: the autouse guard really is installed for this module."""
    assert socket.socket.connect.__name__ == "guarded_connect"
    assert socket.create_connection.__name__ == "blocked_create_connection"


class _FakeCallback:
    """Minimal CallbackQuery stand-in for unit-level ``safe_answer_callback`` tests."""

    def __init__(self) -> None:
        self.id = "cb-1"
        self.sent: list[str] = []
        self.from_user = type("U", (), {"id": 7})()
        self.message = type("M", (), {"chat": type("C", (), {"id": 7})()})()
        self.bot = type("B", (), {"send_message": self._send_message})()

    async def answer(self, *args, **kwargs):
        raise stale_query_error()

    async def _send_message(self, chat_id, text, **kwargs):
        self.sent.append(text)
        return True


# ---------------- unit level: what safe.py swallows and what it must not ----------------
async def test_safe_send_returns_false_and_logs_delivery_failure(caplog):
    async def blocked():
        raise forbidden_error()

    with caplog.at_level(logging.ERROR):
        delivered = await safe_send(blocked(), action="unit_blocked", chat_id=1, user_id=2)

    assert delivered is False
    assert len(errors(caplog)) == 1, "one delivery failure → exactly one ERROR line"
    assert actions(caplog) == {"unit_blocked"}
    assert errors(caplog)[0].exc_info is not None, "the traceback must be kept"


async def test_safe_send_reraises_programming_errors(caplog):
    """A TypeError is our bug, not a delivery failure — it must not be swallowed."""

    async def broken():
        raise TypeError("send_message() got an unexpected keyword argument 'nope'")

    with caplog.at_level(logging.ERROR):
        with pytest.raises(TypeError):
            await safe_send(broken(), action="unit_broken")

    assert errors(caplog) == [], "a programming error must not be logged as a send failure"


@pytest.mark.parametrize("exc", [OSError("connection reset by peer"), network_error()])
async def test_safe_send_swallows_transport_failures(exc):
    async def broken():
        raise exc

    assert await safe_send(broken(), action="unit_transport") is False


def test_is_stale_query_error_only_matches_telegram_stale_copy():
    assert is_stale_query_error(stale_query_error()) is True
    assert is_stale_query_error(
        TelegramBadRequest(
            method=_CALLBACK_METHOD,
            message="Bad Request: query ID is invalid",
        )
    ) is True
    # A different 400, a transport error and a plain Python error are not "stale".
    assert is_stale_query_error(
        TelegramBadRequest(
            method=_CALLBACK_METHOD,
            message="Bad Request: message is not modified",
        )
    ) is False
    assert is_stale_query_error(OSError("query is too old")) is False
    assert is_stale_query_error(TypeError("query is too old")) is False


async def test_safe_answer_callback_can_skip_the_stale_notice(caplog):
    """The allowlist path acks silently: a blocked user gets no extra message."""
    callback = _FakeCallback()

    with caplog.at_level(logging.ERROR):
        answered = await safe_answer_callback(
            callback, action="ack", bot=None, notify_stale=False
        )

    assert answered is False
    assert callback.sent == [], "no stale notice when notify_stale=False"


async def test_safe_answer_callback_notices_staleness_by_default(caplog):
    """The default (handler) path warns the user instead of leaving a dead spinner."""
    callback = _FakeCallback()

    with caplog.at_level(logging.ERROR):
        answered = await safe_answer_callback(callback, action="ack")

    assert answered is False
    assert callback.sent == [STALE_CARD_TEXT]
    assert "callback_stale" in actions(caplog)


# ---------------- (а) stale callback query ----------------
async def test_stale_callback_query_does_not_fail_update_and_warns_user(
    harness, monkeypatch, caplog
):
    """A press on an old card must not kill the update — and must not hang the spinner."""
    harness.extraction._results = [full_result()]
    await harness.send_text("ТОО Ромашка, Алматы, +7 700 123 45 67")
    assert await harness.fsm_state() == LeadForm.Collecting.state

    break_method(monkeypatch, harness.bot, "answer_callback_query", stale_query_error())

    with caplog.at_level(logging.ERROR):
        result = await harness.tap(CB_DONE)  # must not raise

    assert result is not UNHANDLED, "the update must be reported as handled (no 500, no retry)"
    # The press reached the flow anyway: the card was extracted and sent.
    assert await harness.fsm_state() == LeadForm.Reviewing.state
    assert harness.bot.contains("Добавить в таблицу?")

    notices = [c for c in harness.bot.messages() if c.text and "Карточка устарела" in c.text]
    assert len(notices) == 1, "the user must get exactly one fresh «Карточка устарела» message"
    notice = notices[0]
    assert notice.chat_id == OWNER_USER_ID
    assert notice.text == STALE_CARD_TEXT
    assert notice.parse_mode == ParseMode.HTML, "the notice carries a premium emoji"
    assert_valid_telegram_html(notice.text)

    stale_records = [r for r in errors(caplog) if getattr(r, "action", None) == "callback_stale"]
    assert stale_records, "the stale query must be logged"
    assert "query is too old" in str(stale_records[0].exc_info[1])
    assert stale_records[0].callback_query_id is not None


async def test_callback_answer_failure_that_is_not_stale_sends_no_notice(
    harness, monkeypatch, caplog
):
    """A blocked-bot 403 on ``answer_callback_query`` is logged, not mistaken for staleness."""
    break_method(monkeypatch, harness.bot, "answer_callback_query", forbidden_error())

    with caplog.at_level(logging.ERROR):
        result = await harness.tap(CB_DONE)

    assert result is not UNHANDLED
    assert not harness.bot.contains("Карточка устарела")
    assert "ack_done" in actions(caplog)


async def test_callback_without_a_message_does_not_fail_update(harness, caplog):
    """``callback.message`` is optional (inaccessible message) — the handler must cope."""
    payload = {
        "update_id": 810001,
        "callback_query": {
            "id": "callback-no-message",
            "chat_instance": "test-chat-instance",
            "from": {"id": OWNER_USER_ID, "is_bot": False, "first_name": "Test"},
            "data": CB_EDIT,
        },
    }

    with caplog.at_level(logging.ERROR):
        result = await harness.feed(payload)

    assert result is not UNHANDLED
    assert await harness.fsm_state() == LeadForm.EditingField.state
    assert errors(caplog) == [], "a message-less callback is legal, not an error"


# ---------------- (б) a failed reply send ----------------
async def test_forbidden_send_does_not_fail_the_update(harness, monkeypatch, caplog):
    """``/start`` for a user who blocked the bot: update succeeds, failure is logged."""
    break_method(monkeypatch, harness.bot, "send_message", forbidden_error())

    with caplog.at_level(logging.ERROR):
        result = await harness.send_command("/start")

    assert result is not UNHANDLED
    assert harness.bot.messages() == [], "nothing was delivered"
    assert "start_greeting" in actions(caplog)
    assert "LeadForge AI" not in harness.bot.texts()


async def test_chat_not_found_in_the_flow_does_not_fail_the_update(harness, monkeypatch, caplog):
    """The collecting prompt goes through ``safe.notify`` too — a dead chat must not 500."""
    break_method(monkeypatch, harness.bot, "send_message", chat_not_found_error())

    with caplog.at_level(logging.ERROR):
        result = await harness.send_text("ТОО Ромашка, Алматы, +7 700 123 45 67")

    assert result is not UNHANDLED
    # The session itself was still created: only the *send* failed.
    sessions = await harness.sessions()
    assert len(sessions) == 1 and sessions[0].status == "collecting"
    assert harness.bot.messages() == []

    records = [r for r in errors(caplog) if getattr(r, "action", None) == "collecting_prompt"]
    assert records, "the failed prompt must be logged with its call site"
    assert records[0].chat_id == OWNER_USER_ID
    assert records[0].telegram_user_id == OWNER_USER_ID


async def test_review_card_send_failure_does_not_fail_the_update(harness, monkeypatch, caplog):
    """The review card is the flakiest send (long HTML + keyboard) — keep the flow alive."""
    harness.extraction._results = [full_result()]
    await harness.send_text("ТОО Ромашка, Алматы, +7 700 123 45 67")

    break_method(monkeypatch, harness.bot, "send_message", forbidden_error())

    with caplog.at_level(logging.ERROR):
        result = await harness.send_command("/done")

    assert result is not UNHANDLED
    assert await harness.fsm_state() == LeadForm.Reviewing.state
    assert "review_card" in actions(caplog)


# ---------------- (в) allowlist middleware ----------------
async def test_allowlist_reply_failure_does_not_fail_the_update(harness, monkeypatch, caplog):
    break_method(monkeypatch, harness.bot, "send_message", chat_not_found_error())

    with caplog.at_level(logging.ERROR):
        result = await harness.send_command("/start", user_id=STRANGER_USER_ID)

    assert result is not UNHANDLED
    assert "allowlist_reply" in actions(caplog)
    assert harness.extraction.calls == [], "blocked users must never reach extraction"
    assert await harness.lead_count() == 0


async def test_allowlist_callback_ack_failure_does_not_fail_the_update(
    harness, monkeypatch, caplog
):
    break_method(monkeypatch, harness.bot, "answer_callback_query", forbidden_error())

    with caplog.at_level(logging.ERROR):
        result = await harness.tap(CB_ADD, user_id=STRANGER_USER_ID)

    assert result is not UNHANDLED
    assert "allowlist_callback_ack" in actions(caplog)
    assert not harness.bot.contains("Карточка устарела")
    assert harness.bot.contains("Это приватный бот.")
    assert await harness.lead_count() == 0


async def test_allowlist_stale_callback_is_logged_but_never_notices_a_blocked_user(
    harness, monkeypatch, caplog
):
    """A blocked user pressing an old button: no crash, and no extra message either."""
    break_method(monkeypatch, harness.bot, "answer_callback_query", stale_query_error())

    with caplog.at_level(logging.ERROR):
        result = await harness.tap(CB_ADD, user_id=STRANGER_USER_ID)

    assert result is not UNHANDLED
    # Staleness is the more specific label, so it wins over the call site's action.
    assert "callback_stale" in actions(caplog)
    assert not harness.bot.contains("Карточка устарела")
    assert harness.bot.contains("Это приватный бот.")
    assert await harness.lead_count() == 0


# ---------------- (г) real bugs stay visible ----------------
async def test_programming_error_still_reaches_the_global_handler(harness, monkeypatch, caplog):
    """Swapping the send for one that raises ``TypeError`` must not be silenced."""
    break_method(
        monkeypatch,
        harness.bot,
        "send_message",
        TypeError("send_message() got an unexpected keyword argument 'nope'"),
    )

    with caplog.at_level(logging.ERROR):
        result = await harness.send_command("/start")  # must not raise

    handlers = [r for r in errors(caplog) if "unhandled exception" in r.getMessage()]
    assert handlers, "TypeError must reach app.bot.errors.handle_update_error"
    assert "TypeError" in handlers[0].getMessage()
    assert handlers[0].exc_info is not None and handlers[0].exc_info[0] is TypeError
    assert getattr(handlers[0], "action", None) == "update_error"
    assert getattr(handlers[0], "update_id", None) is not None
    # The error handler reports the update as handled, which is what keeps the
    # webhook at 200 instead of the old retry storm...
    assert result is not UNHANDLED
    # ...and the bug is *not* mislabelled as a delivery failure.
    assert "start_greeting" not in actions(caplog)


# ---------------- the webhook route itself (no 500, ever) ----------------
def _start_payload(user_id: int = OWNER_USER_ID) -> dict:
    return {
        "update_id": 900001,
        "message": {
            "message_id": 1,
            "date": BASE_DATE,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "text": "/start",
        },
    }


async def _post_webhook(harness, payload: dict):
    """POST *payload* to the real ``app.main`` app over ASGI (the path Telegram hits).

    Hand-building a ``Request`` and calling the route function would hide a broken
    route signature; the container is wired in through ``app.main._container`` by
    the helper, exactly as production does it.
    """
    from tests.integration_harness import post_webhook

    return await post_webhook(harness, json.dumps(payload))


def _retry_middleware(container) -> RetryMiddleware:
    """The harness wires RetryMiddleware as an *inner* update middleware."""
    for middleware in container.dp.update.middleware:
        if isinstance(middleware, RetryMiddleware):
            return middleware
    raise AssertionError("RetryMiddleware is not registered on the dispatcher")


async def test_webhook_returns_200_when_the_reply_cannot_be_delivered(harness, monkeypatch, caplog):
    break_method(monkeypatch, harness.bot, "send_message", forbidden_error())

    with caplog.at_level(logging.ERROR):
        response = await _post_webhook(harness, _start_payload())

    assert response.status_code == 200, "Telegram would replay the update on any non-2xx"
    assert response.json() == {"ok": True}
    assert "start_greeting" in actions(caplog)


async def test_webhook_returns_200_when_the_handler_has_a_bug(harness, monkeypatch, caplog):
    """``dp.errors`` returning True means the route never sees the exception."""
    break_method(monkeypatch, harness.bot, "send_message", TypeError("bad kwarg"))

    with caplog.at_level(logging.ERROR):
        response = await _post_webhook(harness, _start_payload())

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert any("unhandled exception" in r.getMessage() for r in errors(caplog))


async def test_webhook_returns_200_when_the_retry_middleware_gives_up(harness, monkeypatch, caplog):
    """A ``TelegramNetworkError`` escaping a handler: retries exhausted → still 200.

    ``safe.*`` already absorbs transport failures on outgoing sends, so this
    exercises the other, rarer path — an error raised *outside* a send call that
    has to travel through ``RetryMiddleware`` (which re-raises once it is out of
    attempts) to the global handler.
    """
    calls = 0

    async def always_down(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise network_error()

    # /last reads the DB through container.leads — make that read "the network is down".
    monkeypatch.setattr(harness.container.leads, "get_last_leads", always_down)
    monkeypatch.setattr(_retry_middleware(harness.container), "max_retries", 0)

    with caplog.at_level(logging.ERROR):
        response = await _post_webhook(
            harness,
            {
                "update_id": 900002,
                "message": {
                    "message_id": 2,
                    "date": BASE_DATE,
                    "chat": {"id": OWNER_USER_ID, "type": "private"},
                    "from": {"id": OWNER_USER_ID, "is_bot": False, "first_name": "Test"},
                    "text": "/last",
                },
            },
        )

    assert calls == 1, "max_retries=0 means no retry is attempted"
    assert response.status_code == 200
    assert any("unhandled exception" in r.getMessage() for r in errors(caplog))


# ---------------- a failed confirmation must not cost the user their lead ----------------
async def test_add_lead_still_saves_when_the_confirmation_send_fails(harness, monkeypatch, caplog):
    harness.extraction._results = [full_result()]
    await harness.send_text("ТОО Ромашка, Алматы, +7 700 123 45 67")
    await harness.send_command("/done")

    break_method(monkeypatch, harness.bot, "send_message", forbidden_error())

    with caplog.at_level(logging.ERROR):
        # FIX-24: the confirmation is sent by the tracked sync job (the first moment
        # both the ID and the row number are known), so the *update* stays successful
        # and the failed send is reported by that job — still never a 500.
        result = await harness.tap(CB_ADD)
        assert await wait_until(lambda: "lead_added" in actions(caplog)), (
            "the failed confirmation was not reported"
        )

    assert result is not UNHANDLED
    leads = await harness.leads()
    assert len(leads) == 1 and leads[0].company_name == "Ромашка"
    assert "lead_added" in actions(caplog)
    assert await harness.fsm_state() is None
