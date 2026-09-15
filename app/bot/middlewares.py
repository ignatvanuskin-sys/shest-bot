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
    """Allowlist decision for the env source (also a testable seam)."""
    return user_id in allowed_user_ids


# Where an admission came from — reported in the log so «why does this user have
# access?» is answerable from the JSON lines alone (FIX-23).
ALLOW_SOURCE_ENV = "env"
ALLOW_SOURCE_USERS_TABLE = "users_table"


class AllowlistMiddleware(BaseMiddleware):
    """Blocks unknown users with a single neutral reply; logs their telegram_id.

    Access is granted when the id is in ``ALLOWED_USER_IDS`` *or* in the ``users``
    table (FIX-23). The ``users`` table used to be written by nothing and read by
    nothing; now adding a row is a second, persistent source of access — and the
    source that admitted a user is logged once per process.
    """

    def __init__(self, container):
        super().__init__()
        self._container = container
        self._warned: set[int] = set()
        # user_id → the source they were admitted by, so one busy user does not
        # produce one INFO line per update (the log stays readable).
        self._admitted: dict[int, str] = {}

    async def _allow_source(self, user_id: int) -> str | None:
        """``"env"``, ``"users_table"``, or None when access is denied."""
        if is_allowed(self._container.settings.allowed_user_ids, user_id):
            return ALLOW_SOURCE_ENV
        try:
            leads = getattr(self._container, "leads", None)
            lookup = getattr(leads, "is_allowed_user", None)
            if lookup is not None and await lookup(user_id):
                return ALLOW_SOURCE_USERS_TABLE
        except Exception:
            # A broken lookup (DB locked/not migrated yet) must not grant access —
            # and must not turn every update into a 500 either. The env allowlist
            # above stays the primary, always-working source.
            logger.exception("allowlist lookup in the users table failed")
        return None

    def _log_admission(self, user_id: int, source: str) -> None:
        if self._admitted.get(user_id) == source:
            return
        self._admitted[user_id] = source
        log_json(
            logger, 20, "access allowed",
            telegram_user_id=user_id, action="allowlist_allowed", source=source,
        )

    async def __call__(self, handler, event, data):
        user_event = unwrap_user_event(event)
        user_id = _extract_user_id(user_event) if user_event is not None else None
        if user_id is None:
            return await handler(event, data)

        source = await self._allow_source(user_id)
        if source is not None:
            self._log_admission(user_id, source)
            return await handler(event, data)

        if user_id not in self._warned:
            self._warned.add(user_id)
            if not self._container.settings.allowed_user_ids:
                # Empty env allowlist → log with an explicit mark so the owner can grab
                # the id (the users table was consulted and did not know it either).
                log_json(
                    logger, 30,
                    "blocked: allowlist empty, add this id to ALLOWED_USER_IDS "
                    "or insert it into the users table",
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
