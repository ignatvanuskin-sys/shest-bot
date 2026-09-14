"""aiogram middlewares: allowlist + Telegram retry/backoff."""
from __future__ import annotations

import asyncio
import logging

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.logging_config import log_json

logger = logging.getLogger(__name__)


def _extract_user_id(event: TelegramObject) -> int | None:
    if isinstance(event, Message):
        return event.from_user.id if event.from_user else None
    if isinstance(event, CallbackQuery):
        return event.from_user.id if event.from_user else None
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
        user_id = _extract_user_id(event)
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
            await self._container.bot.send_message(user_id, "Это приватный бот.")
        if isinstance(event, CallbackQuery):
            try:
                await self._container.bot.answer_callback_query(event.id)
            except Exception:
                pass
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
