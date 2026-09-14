"""Dispatcher wiring: router registration, middlewares, webhook helpers."""
from __future__ import annotations

import logging

from aiogram.types import Update

from app.bot.handlers import router
from app.bot.middlewares import AllowlistMiddleware, RetryMiddleware

logger = logging.getLogger(__name__)


def setup_dispatcher(container) -> None:
    dp = container.dp
    dp.workflow_data.update({"container": container})

    # Order: outer retry, then allowlist (innermost runs last).
    dp.update.middleware(RetryMiddleware(max_retries=3))
    dp.update.middleware(AllowlistMiddleware(container))

    dp.include_router(router)
    logger.info("dispatcher configured")


async def set_webhook(container, webhook_url: str, secret_token: str | None = None) -> bool:
    """Register the Telegram webhook; returns True on success."""
    from aiogram.types import BotCommand

    await container.bot.set_my_commands(
        [
            BotCommand(command="start", description="Начало работы"),
            BotCommand(command="new", description="Новый лид"),
            BotCommand(command="done", description="Завершить сбор"),
            BotCommand(command="cancel", description="Отменить"),
            BotCommand(command="last", description="Последние лиды"),
            BotCommand(command="search", description="Поиск"),
            BotCommand(command="stats", description="Статистика"),
            BotCommand(command="undo", description="Откатить"),
            BotCommand(command="settings", description="Настройки"),
            BotCommand(command="help", description="Помощь"),
        ]
    )
    await container.bot.set_webhook(
        url=webhook_url,
        secret_token=secret_token or None,
        drop_pending_updates=True,
    )
    logger.info("webhook set to %s", webhook_url)
    return True


async def feed_update(container, update: Update) -> None:
    """Feed a raw Telegram update into the dispatcher (used by the webhook route)."""
    await container.dp.feed_update(container.bot, update)
