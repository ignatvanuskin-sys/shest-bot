"""Application entrypoint: FastAPI webhook (production) or long polling (dev)."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.bot.dispatcher import feed_update, set_webhook, setup_dispatcher
from app.config import get_settings
from app.database import run_migrations_async
from app.di import build_container
from app.logging_config import setup_logging
from app.services.background import start_background_workers
from app.services.startup import reconcile_hung_sessions

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8080
WEBHOOK_PATH = "/webhook"

_container = None


def resolve_port() -> int:
    """HTTP port for uvicorn. Railway (and most PaaS) inject ``PORT``."""
    raw = os.environ.get("PORT", "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        return int(raw)
    except ValueError:
        logger.warning("invalid PORT=%r — falling back to %s", raw, DEFAULT_PORT)
        return DEFAULT_PORT


def resolve_webhook_url(settings) -> tuple[str, str] | None:
    """Resolve the webhook URL and where it came from.

    Priority: explicit ``WEBHOOK_URL``, then the public domain Railway injects
    (``RAILWAY_PUBLIC_DOMAIN``) with the ``/webhook`` path appended. Returns
    ``None`` when neither is configured.
    """
    explicit = (settings.WEBHOOK_URL or "").strip()
    if explicit:
        return explicit, "WEBHOOK_URL"

    domain = (settings.RAILWAY_PUBLIC_DOMAIN or "").strip()
    if not domain:
        return None
    if not domain.startswith(("http://", "https://")):
        domain = f"https://{domain}"
    return f"{domain.rstrip('/')}{WEBHOOK_PATH}", "RAILWAY_PUBLIC_DOMAIN"


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
    await run_migrations_async()
    # Sessions whose dialog lived in memory died with the previous process: close
    # them out now, otherwise they stay «collecting»/«review» for ever (FIX-9).
    await reconcile_hung_sessions(container.leads)
    settings = container.settings
    if settings.DEV_POLLING:
        logger.info("DEV_POLLING=true — long polling mode, webhook is not registered")
        return

    resolved = resolve_webhook_url(settings)
    if resolved is None:
        logger.warning(
            "webhook mode but neither WEBHOOK_URL nor RAILWAY_PUBLIC_DOMAIN is set — "
            "webhook not registered; set WEBHOOK_URL to the public https URL of this service"
        )
        return

    webhook_url, source = resolved
    logger.info("registering webhook at %s (source: %s)", webhook_url, source)
    try:
        await set_webhook(container, webhook_url, settings.WEBHOOK_SECRET)
    except Exception as exc:
        # Do not crash-loop the container: log loudly and let /health keep serving.
        logger.error(
            "webhook registration FAILED for %s (source: %s): %s", webhook_url, source, exc
        )
        return
    logger.info("webhook registration OK: %s (source: %s)", webhook_url, source)


@asynccontextmanager
async def lifespan(app: FastAPI):
    container = init_app()
    await startup_runtime(container)
    start_background_workers(container)
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
    await startup_runtime(container)
    start_background_workers(container)
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

        port = resolve_port()
        logger.info("starting uvicorn on 0.0.0.0:%s", port)
        uvicorn.run("app.main:app", host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
