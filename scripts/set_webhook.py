"""Register (or clear) the Telegram webhook.

Usage:
    python -m scripts.set_webhook             # set webhook from WEBHOOK_URL
    python -m scripts.set_webhook --clear     # remove the webhook (e.g. before polling)
"""
from __future__ import annotations

import argparse
import asyncio

from aiogram import Bot

from app.config import get_settings


async def main(clear: bool = False) -> None:
    settings = get_settings()
    if not settings.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is empty")
    bot = Bot(settings.BOT_TOKEN)
    if clear:
        await bot.delete_webhook(drop_pending_updates=True)
        print("webhook deleted")
    else:
        if not settings.WEBHOOK_URL:
            raise SystemExit("WEBHOOK_URL is empty — set it in .env")
        await bot.set_webhook(
            url=settings.WEBHOOK_URL,
            secret_token=settings.WEBHOOK_SECRET or None,
            drop_pending_updates=True,
        )
        info = await bot.get_webhook_info()
        print(f"webhook set -> {info.url}")
    await bot.session.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--clear", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(clear=args.clear))
