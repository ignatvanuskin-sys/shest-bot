"""FIX-9: a restart must not leave sessions «collecting» for ever.

The dialog state (aiogram ``MemoryStorage``) and the message buffer live in memory, so
after a container restart the ``lead_sessions`` rows left in ``collecting``/``review``
are dead ends — the text they were collecting is gone. Startup now closes them out and
logs what it closed, so the database describes what actually happened.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from app.main import startup_runtime
from app.models import LeadSession
from app.services.lead_service import (
    UNFINISHED_SESSION_STATUSES,
    LeadService,
)
from app.services.startup import reconcile_hung_sessions
from tests.integration_harness import (
    harness,  # noqa: F401  — imported fixture
    no_network,  # noqa: F401  — autouse imported fixture (blocks non-loopback sockets)
)


async def _seed_session(session_factory, user_id: int, status: str) -> int:
    async with session_factory() as session:
        row = LeadSession(telegram_user_id=user_id, status=status, combined_text="незавершённый текст")
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row.id


async def _statuses(session_factory) -> dict[int, str]:
    async with session_factory() as session:
        result = await session.execute(select(LeadSession))
        return {row.id: row.status for row in result.scalars().all()}


OPEN_STATUSES = ("collecting", "review", "editing")


async def test_cancel_unfinished_sessions_marks_only_interrupted_ones(session_factory):
    open_ids = [await _seed_session(session_factory, 1, status) for status in OPEN_STATUSES]
    finished = await _seed_session(session_factory, 1, "done")
    already = await _seed_session(session_factory, 2, "cancelled")

    closed = await LeadService(session_factory).cancel_unfinished_sessions()

    assert closed == open_ids
    statuses = await _statuses(session_factory)
    for session_id in open_ids:
        assert statuses[session_id] == "cancelled"
    assert statuses[finished] == "done", "a finished dialog is not touched"
    assert statuses[already] == "cancelled"


async def test_reconcile_hung_sessions_logs_count_and_ids(session_factory, caplog):
    first = await _seed_session(session_factory, 1, "collecting")
    second = await _seed_session(session_factory, 2, "review")
    await _seed_session(session_factory, 3, "done")

    with caplog.at_level(logging.WARNING, logger="app.services.startup"):
        ids = await reconcile_hung_sessions(LeadService(session_factory))

    assert ids == [first, second]
    records = [r for r in caplog.records if getattr(r, "action", None) == "sessions_reconciled"]
    assert records, "the reconciliation must be logged"
    assert records[0].count == 2
    assert records[0].session_ids == [first, second]
    assert "restart" in records[0].getMessage()


async def test_reconcile_hung_sessions_is_idempotent(session_factory):
    await _seed_session(session_factory, 1, "collecting")
    leads = LeadService(session_factory)

    assert len(await reconcile_hung_sessions(leads)) == 1
    assert await reconcile_hung_sessions(leads) == [], "nothing is opened a second time"


def test_unfinished_statuses_cover_the_dialog_lifecycle():
    assert set(UNFINISHED_SESSION_STATUSES) == {"collecting", "review", "editing"}
    assert "done" not in UNFINISHED_SESSION_STATUSES
    assert "cancelled" not in UNFINISHED_SESSION_STATUSES


# ---------------- through the startup path ----------------


async def test_startup_runtime_closes_sessions_left_by_the_previous_process(harness):
    """End-to-end «restart»: rows written by the previous process are closed at boot."""
    leads = harness.container.leads
    hung = await leads.create_session(424242)
    await leads.update_session(hung, status="review", combined_text="текст из прошлой жизни")
    finished = await leads.create_session(424242)
    await leads.update_session(finished, status="done")

    await startup_runtime(harness.container)

    sessions = {row.id: row for row in await harness.sessions()}
    assert sessions[hung].status == "cancelled", "a hung session stays as if it were live"
    assert sessions[hung].combined_text == "текст из прошлой жизни", (
        "the reconciliation must not destroy the collected text"
    )
    assert sessions[finished].status == "done"


async def test_startup_runtime_survives_no_sessions(harness):
    """The very first boot (empty table) must not raise."""
    await startup_runtime(harness.container)

    assert await harness.sessions() == []
