"""Failure-tolerant Telegram I/O — the seam between "message failed" and "update failed".

Telegram re-delivers every update whose webhook response is not 2xx. So a single
unsuccessful send (the owner blocked the bot, ``chat not found``, a stale
callback query after a bot restart) must never bubble out of a handler: it would
turn into an HTTP 500 and Telegram would replay the same update over and over.

Every user-facing send in the bot goes through this module. A delivery failure
is logged at ERROR level with the full traceback and a per-call-site ``action``
(so it stays visible and greppable in the JSON logs), and the caller keeps
running — the update is reported as handled and Telegram gets its 200.

This deliberately does *not* hide programming errors: a ``TypeError`` or a bad
kwarg in a send call still raises (and is caught by the global error handler in
``app.bot.errors``). Only Telegram/network I/O failures are swallowed here.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable

from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message

from app.bot.premium import emoji

logger = logging.getLogger(__name__)

# "The message did not reach the user", not "our code is broken". TelegramAPIError
# covers every Bot API answer (BadRequest, Forbidden, NotFound, RetryAfter,
# server errors) and aiogram wraps aiohttp timeouts/connection errors into
# TelegramNetworkError, a TelegramAPIError subclass. OSError covers the rest of
# the socket layer.
SEND_ERRORS = (TelegramAPIError, OSError)

# Telegram's 400 answer when the callback query is gone: the button was pressed
# on a card that predates a bot restart, or the client retried an old press.
STALE_QUERY_MARKERS = ("query is too old", "query id is invalid")

# Sent as a *new* message when the card the button belongs to can no longer be
# acknowledged or edited (sent with parse_mode=HTML — it carries a premium emoji).
STALE_CARD_TEXT = f"{emoji('time')} Карточка устарела — пришлите лид заново."


def is_stale_query_error(exc: BaseException) -> bool:
    """True for Telegram's "query is too old ... or query ID is invalid" 400."""
    if not isinstance(exc, TelegramAPIError):
        return False
    message = (getattr(exc, "message", None) or str(exc)).lower()
    return any(marker in message for marker in STALE_QUERY_MARKERS)


def _log_send_failure(
    message: str,
    exc: BaseException,
    *,
    action: str,
    chat_id: int | None,
    user_id: int | None,
    callback_query_id: str | None = None,
) -> None:
    """ERROR log with traceback + structured fields, so the JSON line is greppable."""
    fields: dict[str, Any] = {
        "action": action,
        "error_type": f"{type(exc).__module__}.{type(exc).__qualname__}",
        "reason": str(exc),
    }
    if chat_id is not None:
        fields["chat_id"] = chat_id
    if user_id is not None:
        fields["telegram_user_id"] = user_id
    if callback_query_id is not None:
        fields["callback_query_id"] = callback_query_id
    # exc_info must be a logging keyword (not ``extra``) so the formatter
    # renders the traceback; JsonFormatter picks it up as "exc_info".
    logger.log(logging.ERROR, message, extra=fields, exc_info=exc)


async def safe_send(
    call: Awaitable[Any],
    *,
    action: str,
    chat_id: int | None = None,
    user_id: int | None = None,
    callback_query_id: str | None = None,
) -> bool:
    """Await a Telegram call, swallowing delivery failures. ``True`` if it went out.

    ``call`` is the ready-made awaitable (``bot.send_message(...)`` /
    ``message.answer(...)`` / ``callback.answer()``) — the failure happens inside
    the ``await``, which is exactly what this guards.
    """
    try:
        await call
        return True
    except SEND_ERRORS as exc:
        _log_send_failure(
            "outgoing Telegram send failed — update is treated as handled "
            "(no 500, no Telegram retry)",
            exc,
            action=action,
            chat_id=chat_id,
            user_id=user_id,
            callback_query_id=callback_query_id,
        )
        return False


async def notify(
    container,
    chat_id: int,
    text: str,
    *,
    action: str,
    user_id: int | None = None,
    **kwargs: Any,
) -> bool:
    """Send a user-facing message through ``container.bot`` without failing the update."""
    return await safe_send(
        container.bot.send_message(chat_id, text, **kwargs),
        action=action,
        chat_id=chat_id,
        user_id=user_id,
    )


async def safe_reply(message: Message, text: str, *, action: str, **kwargs: Any) -> bool:
    """Answer to an incoming message without failing the update on a bad send."""
    return await safe_send(
        message.answer(text, **kwargs),
        action=action,
        chat_id=message.chat.id,
        user_id=message.from_user.id if message.from_user else None,
    )


def callback_chat_id(callback: CallbackQuery) -> int | None:
    """Chat to talk to about a callback: its message's chat, else the user's DM."""
    message = callback.message
    if message is not None:
        return message.chat.id
    return callback.from_user.id if callback.from_user else None


async def safe_answer_callback(
    callback: CallbackQuery,
    *,
    action: str,
    bot: Any = None,
    notify_stale: bool = True,
) -> bool:
    """``callback.answer()`` that survives a stale/expired query id.

    Telegram answers a press on an old card (bot restarted in between, client
    retry) with 400 "query is too old and response timeout expired or query ID is
    invalid". The press itself still reached the bot, so the handler must keep
    running: the failure is logged (ERROR, ``action="callback_stale"``) and — best
    effort — the user gets a fresh «Карточка устарела — пришлите лид заново»
    message instead of a spinner that never stops.
    """
    try:
        await callback.answer()
        return True
    except SEND_ERRORS as exc:
        stale = is_stale_query_error(exc)
        _log_send_failure(
            "stale callback query: the card is gone for Telegram"
            if stale
            else "callback answer failed — update is treated as handled",
            exc,
            action="callback_stale" if stale else action,
            chat_id=callback_chat_id(callback),
            user_id=callback.from_user.id if callback.from_user else None,
            callback_query_id=callback.id,
        )
        if stale and notify_stale:
            chat_id = callback_chat_id(callback)
            if chat_id is not None:
                await safe_send(
                    (bot or callback.bot).send_message(
                        chat_id, STALE_CARD_TEXT, parse_mode=ParseMode.HTML
                    ),
                    action="stale_card_notice",
                    chat_id=chat_id,
                    user_id=callback.from_user.id if callback.from_user else None,
                    callback_query_id=callback.id,
                )
        return False
