"""Dependency container wiring all services together."""
from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from app.config import Settings, get_settings
from app.database import create_engine_and_sessionmaker
from app.logging_config import log_json
from app.services.background import BackgroundTasks
from app.services.dedup import DedupService
from app.services.extraction import ExtractionService
from app.services.lead_service import LeadService
from app.services.session_buffer import SessionBufferService
from app.services.sheets import build_sheets_service

logger = logging.getLogger(__name__)


class Container:
    """Holds all long-lived objects: bot, dispatcher, engine, and services."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.engine, self.session_factory = create_engine_and_sessionmaker(
            self.settings.DATABASE_URL
        )
        self.bot = Bot(token=self.settings.BOT_TOKEN)
        self.dp = Dispatcher(storage=MemoryStorage())
        # Keeps strong references to fire-and-forget jobs (sheet syncs, the periodic
        # resync worker) and drains them on shutdown.
        self.tasks = BackgroundTasks()

        self.session_buffer = SessionBufferService(self.settings.COLLECT_TIMEOUT_SECONDS)
        self.leads = LeadService(self.session_factory)
        self.extraction = ExtractionService(
            self.settings.OPENROUTER_API_KEY,
            self.session_factory,
            primary_model=self.settings.OPENROUTER_MODEL,
            fallback_model=self.settings.OPENROUTER_FALLBACK_MODEL,
        )
        self.dedup = DedupService(
            self.session_factory,
            candidate_limit=self.settings.DEDUP_CANDIDATE_LIMIT,
        )
        self.sheets = build_sheets_service(self.settings)

    def warn_missing_secrets(self) -> None:
        missing = self.settings.missing_secrets()
        if missing:
            log_json(
                logger, 30, "WARNING: missing configuration",
                action="missing_secrets", reason=", ".join(missing),
            )
        else:
            log_json(logger, 20, "configuration complete", action="config_ok")

    async def close(self) -> None:
        # Order matters: in-flight sheet syncs must finish (and get their notification
        # out) before the clients and the engine they use are torn down.
        await self.tasks.shutdown()
        await self.session_buffer.shutdown()
        await self.extraction.close()
        await self.sheets.close()
        await self.bot.session.close()
        await self.engine.dispose()


def build_container(settings: Settings | None = None) -> Container:
    return Container(settings)
