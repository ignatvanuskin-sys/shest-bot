"""Application entrypoint: FastAPI webhook (production) or long polling (dev)."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.bot.dispatcher import feed_update, set_webhook, setup_dispatcher
from app.config import get_settings
from app.database import run_migrations
from app.di import build_container
from app.logging_config import setup_logging

logger = logging.getLogger(__name__)

_container = None


def init_app(settings=None):
    """Build and wire the container + dispatcher. Does NOT start polling/webhook."""
    global _container
    settings = settings or get_settings()
    setup_logging()
    container = build_container(settings)
    container.warn_missing_secrets()
    setup_dispatcher(container)
    _container = container
    return container


async def startup_runtime(container) -> None:
    """Run migrations and register the webhook when in webhook mode."""
    run_migrations()
    settings = container.settings
    if not settings.DEV_POLLING:
        if settings.WEBHOOK_URL:
            await set_webhook(container, settings.WEBHOOK_URL, settings.WEBHOOK_SECRET)
        else:
            logger.warning("webhook mode but WEBHOOK_URL is empty — webhook not registered")


@asynccontextmanager
async def lifespan(app: FastAPI):
    container = init_app()
    await startup_runtime(container)
    yield
    await container.close()


app = FastAPI(title="LeadForge AI", lifespan=lifespan)


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.post("/webhook")
async def webhook(update: dict, request: Request) -> JSONResponse:
    container = _container
    if container is None:
        raise HTTPException(status_code=503, detail="not initialised")

    secret = container.settings.WEBHOOK_SECRET
    if secret and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret:
        raise HTTPException(status_code=403, detail="bad secret")

    from aiogram.types import Update

    try:
        await feed_update(container, Update.model_validate(update))
    except Exception:
        logger.exception("webhook update handling failed")
        raise HTTPException(status_code=500, detail="internal")
    return JSONResponse({"ok": True})


async def run_polling() -> None:
    container = init_app()
    run_migrations()
    logger.info("starting long polling (DEV_POLLING)")
    try:
        await container.dp.start_polling(container.bot, drop_pending_updates=True)
    finally:
        await container.close()


def main() -> None:
    settings = get_settings()
    if settings.DEV_POLLING:
        asyncio.run(run_polling())
    else:
        import uvicorn

        uvicorn.run("app.main:app", host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
