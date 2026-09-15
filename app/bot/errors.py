"""Global aiogram error handler — the last line of defence against the retry storm.

Telegram re-delivers any update whose webhook response is not 2xx, so one
unexpected exception in a handler previously became an HTTP 500 and Telegram
replayed the same update in a loop. This handler (registered on ``dp.errors``,
which aiogram's ``ErrorsMiddleware`` feeds with every exception escaping the
update pipeline — handlers *and* middlewares) does two things:

1. logs the exception at ERROR level with the full traceback and as much update
   context as the update carries (type, update_id, user id, callback data,
   chat id) — real bugs stay loud and debuggable;
2. returns ``True``, which marks the update as handled: the dispatcher returns
   normally and the webhook answers 200.
"""
from __future__ import annotations

import logging
from typing import Any

from aiogram.types import CallbackQuery, ErrorEvent, Message, Update

from app.bot.middlewares import unwrap_user_event

logger = logging.getLogger(__name__)


def update_log_context(update: Update) -> dict[str, Any]:
    """Everything the update itself can tell us about where the error happened."""
    context: dict[str, Any] = {
        "update_id": getattr(update, "update_id", None),
    }
    try:
        context["update_type"] = update.event_type
    except Exception:  # unknown update type — aiogram raises on lookup
        context["update_type"] = "unknown"

    event = unwrap_user_event(update)
    if isinstance(event, CallbackQuery):
        context["telegram_user_id"] = event.from_user.id if event.from_user else None
        context["callback_data"] = event.data
        if event.message is not None:
            context["chat_id"] = event.message.chat.id
    elif isinstance(event, Message):
        context["telegram_user_id"] = event.from_user.id if event.from_user else None
        context["chat_id"] = event.chat.id
    return {key: value for key, value in context.items() if value is not None}


async def handle_update_error(event: ErrorEvent) -> bool:
    """Log an escaped exception in full and report the update as handled."""
    exc = event.exception
    error_type = f"{type(exc).__module__}.{type(exc).__qualname__}"
    context = update_log_context(event.update)
    logger.error(
        "unhandled exception while processing update (%s: %s) — update reported as "
        "handled so Telegram does not retry it",
        error_type,
        exc,
        exc_info=exc,
        extra={**context, "error_type": error_type, "action": "update_error"},
    )
    return True
