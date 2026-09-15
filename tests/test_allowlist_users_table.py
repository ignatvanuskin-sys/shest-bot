"""FIX-23: access comes from ``ALLOWED_USER_IDS`` **or** from the ``users`` table.

The ``users`` table was declared, migrated and never used: the allowlist was read
from the env var alone, so onboarding a colleague meant editing the deployment
configuration. Now a row in ``users`` grants access too, and the middleware logs
*which* source admitted a user (env or table) — «why does this id have access?»
must be answerable from the JSON logs alone.
"""
from __future__ import annotations

import logging

import pytest
from sqlalchemy import select

from app.bot.middlewares import (
    ALLOW_SOURCE_ENV,
    ALLOW_SOURCE_USERS_TABLE,
    AllowlistMiddleware,
    is_allowed,
)
from app.bot import middlewares as mw_mod
from app.models import User
from app.services.lead_service import LeadService
from tests.conftest import FakeContainer
from tests.integration_harness import (
    OWNER_USER_ID,
    STRANGER_USER_ID,
    harness,  # noqa: F401  — imported fixture
    no_network,  # noqa: F401  — autouse imported fixture (blocks non-loopback sockets)
)


# ---------------- the table itself ----------------
@pytest.mark.asyncio
async def test_is_allowed_user_reads_the_users_table(session_factory):
    svc = LeadService(session_factory)
    async with session_factory() as session:
        session.add(User(telegram_user_id=424242, display_name="Owner", role="owner"))
        await session.commit()

    assert await svc.is_allowed_user(424242) is True
    assert await svc.is_allowed_user(999999) is False


@pytest.mark.asyncio
async def test_is_allowed_user_is_false_on_an_empty_table(session_factory):
    assert await LeadService(session_factory).is_allowed_user(1) is False


# ---------------- the middleware decision ----------------
def _collector(logger_name: str, records: list[logging.LogRecord]) -> logging.Handler:
    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Collector(level=logging.DEBUG)
    target = logging.getLogger(logger_name)
    target.addHandler(handler)
    target.setLevel(logging.DEBUG)
    return handler


def _replace_collector(logger_name: str, handler: logging.Handler) -> logging.Logger:
    logger = logging.getLogger(logger_name)
    logger.removeHandler(handler)
    return logger


async def _run_middleware(container, user_id: int) -> list:
    middleware = AllowlistMiddleware(container)
    handled: list = []

    async def handler(event, data):
        handled.append(event)

    original = mw_mod._extract_user_id
    mw_mod._extract_user_id = lambda event: user_id  # type: ignore[assignment]
    try:
        await middleware(handler, object(), {})
    finally:
        mw_mod._extract_user_id = original  # type: ignore[assignment]
    return handled


@pytest.mark.asyncio
async def test_access_by_env_is_allowed_and_logged_as_env():
    container = FakeContainer()  # FakeSettings.allowed_user_ids == {1}
    records: list[logging.LogRecord] = []
    handler = _collector("app.bot.middlewares", records)
    try:
        handled = await _run_middleware(container, 1)
    finally:
        _replace_collector("app.bot.middlewares", handler)

    assert len(handled) == 1, "an env-listed user must reach the handler"
    admitted = [r for r in records if getattr(r, "action", None) == "allowlist_allowed"]
    assert admitted and admitted[0].source == ALLOW_SOURCE_ENV
    assert admitted[0].telegram_user_id == 1


@pytest.mark.asyncio
async def test_access_by_users_table_is_allowed_and_logged_as_the_table():
    container = FakeContainer()
    container.leads.users_table = {777}
    records: list[logging.LogRecord] = []
    handler = _collector("app.bot.middlewares", records)
    try:
        handled = await _run_middleware(container, 777)
    finally:
        _replace_collector("app.bot.middlewares", handler)

    assert len(handled) == 1, "a user listed in the users table must reach the handler"
    admitted = [r for r in records if getattr(r, "action", None) == "allowlist_allowed"]
    assert admitted and admitted[0].source == ALLOW_SOURCE_USERS_TABLE


@pytest.mark.asyncio
async def test_access_is_denied_when_neither_source_knows_the_user():
    container = FakeContainer()
    container.leads.users_table = {777}

    handled = await _run_middleware(container, 778)

    assert handled == []
    assert any("приватный" in text for _, text, _ in container.bot.sent)


@pytest.mark.asyncio
async def test_a_failing_table_lookup_never_grants_access():
    """A broken lookup (locked DB, missing table) must fail *closed*, not open."""

    class ExplodingLeads:
        async def is_allowed_user(self, telegram_user_id):
            raise RuntimeError("database is locked")

    container = FakeContainer(leads=ExplodingLeads())
    records: list[logging.LogRecord] = []
    handler = _collector("app.bot.middlewares", records)
    try:
        handled = await _run_middleware(container, 555)
    finally:
        _replace_collector("app.bot.middlewares", handler)

    assert handled == [], "a failed lookup must not admit the user"
    assert not [r for r in records if getattr(r, "action", None) == "allowlist_allowed"]
    assert any(r.levelno >= logging.ERROR for r in records), "the failure must be visible"


@pytest.mark.asyncio
async def test_a_failing_table_lookup_still_lets_env_users_through():
    """The env allowlist is the always-working source; it is checked first."""

    class ExplodingLeads:
        async def is_allowed_user(self, telegram_user_id):
            raise RuntimeError("database is locked")

    container = FakeContainer(leads=ExplodingLeads())  # env knows user 1

    assert len(await _run_middleware(container, 1)) == 1


# ---------------- through the real dispatcher ----------------
@pytest.mark.asyncio
async def test_user_added_to_the_table_can_use_the_bot(harness):
    """End-to-end: a row in ``users`` is enough — no env edit, no restart."""
    async with harness.container.session_factory() as session:
        session.add(User(telegram_user_id=STRANGER_USER_ID, display_name="Colleague"))
        await session.commit()

    await harness.send_command("/start", user_id=STRANGER_USER_ID)

    assert harness.bot.contains("LeadForge AI"), "the table row did not grant access"
    assert not harness.bot.contains("Это приватный бот.")


@pytest.mark.asyncio
async def test_user_absent_from_both_sources_is_still_blocked(harness):
    async with harness.container.session_factory() as session:
        session.add(User(telegram_user_id=STRANGER_USER_ID, display_name="Colleague"))
        await session.commit()

    await harness.send_command("/start", user_id=OWNER_USER_ID + 1)

    assert harness.bot.contains("Это приватный бот.")
    assert not harness.bot.contains("LeadForge AI")


@pytest.mark.asyncio
async def test_the_env_allowlist_still_works_through_the_dispatcher(harness):
    async with harness.container.session_factory() as session:
        rows = (await session.execute(select(User))).scalars().all()
    assert rows == [], "this test is about the env source only"

    await harness.send_command("/start", user_id=OWNER_USER_ID)

    assert harness.bot.contains("LeadForge AI")
    assert is_allowed({OWNER_USER_ID}, OWNER_USER_ID) is True
