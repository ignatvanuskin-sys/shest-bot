"""aiogram middlewares: allowlist + Telegram retry/backoff."""
from __future__ import annotations

import asyncio
import logging

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.types import (
    CallbackQuery,
    ChosenInlineResult,
    InlineQuery,
    Message,
    TelegramObject,
    Update,
)
from app.bot.safe import safe_answer_callback, safe_send
from app.logging_config import log_json

logger = logging.getLogger(__name__)

# Update fields that can carry a ``from_user`` we care about.
_USER_EVENT_FIELDS = (
    "message",
    "edited_message",
    "callback_query",
    "inline_query",
    "chosen_inline_result",
)

# Event types that expose ``from_user`` directly.
_USER_EVENT_TYPES = (Message, CallbackQuery, InlineQuery, ChosenInlineResult)


def _extract_user_id(event: TelegramObject) -> int | None:
    if isinstance(event, _USER_EVENT_TYPES):
        return event.from_user.id if event.from_user else None
    return None


def unwrap_user_event(event: TelegramObject) -> TelegramObject | None:
    """Return the nested event that carries ``from_user``.

    ``dp.update`` middlewares receive the whole ``Update``, not the message or
    callback query inside it — without unwrapping, id extraction always returns
    ``None`` and the allowlist silently allows everyone.
    """
    if not isinstance(event, Update):
        return event
    for field in _USER_EVENT_FIELDS:
        nested = getattr(event, field, None)
        if nested is not None:
            return nested
    return None


def is_allowed(allowed_user_ids: set[int], user_id: int) -> bool:
    """Allowlist decision (also a testable seam)."""
    return user_id in allowed_user_ids


class AllowlistMiddleware(BaseMiddleware):
    """Blocks unknown users with a single neutral reply; logs their telegram_id."""

    def __init__(self, container):
        super().__init__()
        self._container = container
        self._warned: set[int] = set()

    async def __call__(self, handler, event, data):
        user_event = unwrap_user_event(event)
        user_id = _extract_user_id(user_event) if user_event is not None else None
        if user_id is None:
            return await handler(event, data)

        if is_allowed(self._container.settings.allowed_user_ids, user_id):
            return await handler(event, data)

        if user_id not in self._warned:
            self._warned.add(user_id)
            if not self._container.settings.allowed_user_ids:
                # Empty allowlist → log with an explicit mark so the owner can grab the id.
                log_json(
                    logger, 30,
                    "blocked: allowlist empty, add this id to ALLOWED_USER_IDS",
                    telegram_user_id=user_id, action="allowlist_empty",
                )
            else:
                log_json(
                    logger, 20, "blocked: unknown user",
                    telegram_user_id=user_id, action="allowlist_blocked",
                )
            # The neutral reply must not fail the update either (prod: chat not
            # found → 500 → Telegram retried the blocked user's updates).
            await safe_send(
                self._container.bot.send_message(user_id, "Это приватный бот."),
                action="allowlist_reply",
                chat_id=user_id,
                user_id=user_id,
            )
        if isinstance(user_event, CallbackQuery):
            # notify_stale=False: a blocked user gets the neutral reply above and
            # nothing else — the «Карточка устарела» notice is for allowlisted users.
            await safe_answer_callback(
                user_event,
                action="allowlist_callback_ack",
                bot=self._container.bot,
                notify_stale=False,
            )
        return


class RetryMiddleware(BaseMiddleware):
    """Retries handler on Telegram network errors with exponential backoff."""

    def __init__(self, max_retries: int = 3):
        super().__init__()
        self.max_retries = max_retries

    async def __call__(self, handler, event, data):
        attempt = 0
        while True:
            try:
                return await handler(event, data)
            except TelegramRetryAfter as exc:
                if attempt >= self.max_retries:
                    raise
                await asyncio.sleep(exc.retry_after)
            except TelegramNetworkError as exc:
                if attempt >= self.max_retries:
                    raise
                await asyncio.sleep(2 ** attempt)
            attempt += 1
