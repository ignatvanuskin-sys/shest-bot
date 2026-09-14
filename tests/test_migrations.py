"""Regression tests: running Alembic migrations from inside a live event loop.

Bug being covered: a synchronous ``run_migrations()`` invoked from an async
entrypoint (``app.main.startup_runtime`` / ``app.main.run_polling``) reached
``alembic/env.py`` -> ``asyncio.run(...)``, which raises
``RuntimeError: asyncio.run() cannot be called from a running event loop``
and made the bot impossible to start in either polling or webhook mode.
"""
from __future__ import annotations

import sqlite3

import pytest

from app.config import get_settings
from app.database import run_migrations, run_migrations_async

EXPECTED_TABLES = {
    "leads",
    "lead_sessions",
    "raw_messages",
    "extraction_logs",
    "users",
    "audit_log",
    "alembic_version",
}


@pytest.fixture
def tmp_database_url(tmp_path, monkeypatch):
    """Point app config (and therefore Alembic) at a throwaway sqlite file."""
    db_path = tmp_path / "migrations_test.db"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    yield url, db_path
    get_settings.cache_clear()


def read_tables(db_path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    finally:
        conn.close()
    return {row[0] for row in rows}


def assert_schema_created(db_path) -> None:
    tables = read_tables(db_path)
    missing = EXPECTED_TABLES - tables
    assert not missing, f"missing tables after migrations: {sorted(missing)}"
    # Prove the schema came from Alembic (not create_all) and reached the head revision.
    conn = sqlite3.connect(db_path)
    try:
        versions = conn.execute("SELECT version_num FROM alembic_version").fetchall()
    finally:
        conn.close()
    assert [row[0] for row in versions] == ["0001"]


async def test_run_migrations_async_inside_running_loop(tmp_database_url):
    """(a) await run_migrations_async() must work while an event loop is running."""
    url, db_path = tmp_database_url
    assert get_settings().DATABASE_URL == url

    await run_migrations_async()

    assert_schema_created(db_path)


async def test_run_migrations_async_is_idempotent(tmp_database_url):
    """Startup runs migrations every boot, so a second run must not raise."""
    _, db_path = tmp_database_url

    await run_migrations_async()
    await run_migrations_async()

    assert_schema_created(db_path)


async def test_sync_run_migrations_inside_running_loop(tmp_database_url):
    """(b) a direct sync run_migrations() from inside a running loop must not raise."""
    _, db_path = tmp_database_url

    def call_sync() -> None:
        # Executed on the loop thread on purpose: exactly the old failure mode.
        run_migrations()

    call_sync()

    assert_schema_created(db_path)


async def test_startup_runtime_migrates_inside_running_loop(tmp_database_url):
    """The webhook-mode startup path (main.startup_runtime) must survive a live loop.

    DEV_POLLING=True keeps the run free of Telegram network calls / polling.
    """
    from types import SimpleNamespace

    from app.main import startup_runtime

    _, db_path = tmp_database_url
    container = SimpleNamespace(settings=SimpleNamespace(DEV_POLLING=True))

    await startup_runtime(container)

    assert_schema_created(db_path)
