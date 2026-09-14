"""Async SQLAlchemy engine/session factory (SQLite with WAL) + Alembic bootstrap."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def _attach_sqlite_pragmas(engine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_connection, connection_record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()


def create_engine_and_sessionmaker(url: str | None = None):
    """Build an async engine and session factory for a given (or default) URL."""
    target_url = url or get_settings().DATABASE_URL
    engine = create_async_engine(target_url, echo=False, future=True)
    if target_url.startswith("sqlite"):
        _attach_sqlite_pragmas(engine)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, session_factory


# Module-level defaults used by the application runtime.
engine, SessionFactory = create_engine_and_sessionmaker()


async def get_session() -> AsyncSession:
    """FastAPI-style dependency yielding a DB session."""
    async with SessionFactory() as session:
        yield session


def run_migrations() -> None:
    """Run Alembic migrations programmatically (idempotent on startup)."""
    from alembic import command
    from alembic.config import Config

    base_dir = Path(__file__).resolve().parent.parent
    ini_path = base_dir / "alembic.ini"
    if not ini_path.exists():
        logger.warning("alembic.ini not found, skipping migrations")
        return
    cfg = Config(str(ini_path))
    # Point Alembic at the same database as the application.
    cfg.set_main_option("sqlalchemy.url", get_settings().DATABASE_URL)
    cfg.set_main_option("script_location", str(base_dir / "alembic"))
    command.upgrade(cfg, "head")
    logger.info("Alembic migrations applied")


async def run_migrations_async() -> None:
    """Run migrations without blocking/breaking the running event loop.

    Alembic drives an async engine and calls ``asyncio.run`` internally, which is
    illegal from a thread that already owns a live loop. Offloading the blocking
    call to a worker thread keeps startup working in both polling and webhook mode.
    """
    await asyncio.to_thread(run_migrations)
