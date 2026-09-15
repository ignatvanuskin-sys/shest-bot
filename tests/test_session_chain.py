"""FIX-6 regression: one ``session_id`` for the whole chain of a single lead.

``finalize_collection`` used to reset the ContextVar in a ``finally`` that ran right
after the LLM call, i.e. *before* dedup, before the FSM writes and before the
background ``sync_and_notify`` task was created. Log lines from the dedup service,
``LeadService`` and the Sheets sync therefore had no ``session_id``, which is what
makes a single production incident impossible to reconstruct from the JSON logs.

The contract under test:

* the session is bound as soon as a lead session exists (message → LLM);
* it is still bound during dedup and the FSM writes;
* the fire-and-forget sheet sync task inherits it (``asyncio.create_task`` copies the
  context at creation time);
* the user's later action («Добавить», «Объединить») is part of the same chain;
* the previous context value is restored afterwards (no leak into other updates).
"""
from __future__ import annotations

import logging

import pytest

from app.bot import flow
from app.bot.states import LeadForm
from app.logging_config import get_session_id, set_session_id
from app.schemas.extraction import ExtractionResult
from tests.conftest import FakeContainer, FakeDedup, make_fsm
from tests.integration_harness import (
    harness,  # noqa: F401  — imported fixture
    no_network,  # noqa: F401  — autouse imported fixture (blocks non-loopback sockets)
    wait_until,
)

OWNER_USER_ID = 424242
CHAIN_LOGGERS = (
    "app.services.extraction",
    "app.services.dedup",
    "app.services.lead_service",
    "app.services.sheets",
)


def full_result() -> ExtractionResult:
    return ExtractionResult(
        company_name="Ромашка",
        city="Алматы",
        phone_raw="+7 700 123 45 67",
        phone_e164="+77001234567",
        source_guess="2gis",
    )


class SessionCapture(logging.Handler):
    """Records the ContextVar value *at emit time*, exactly like the formatter does."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.entries: list[tuple[logging.LogRecord, str | None]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.entries.append((record, get_session_id()))

    def messages(self, needle: str = "") -> list[str | None]:
        return [sid for record, sid in self.entries if needle in record.getMessage()]


@pytest.fixture
def captured_sessions():
    handler = SessionCapture()
    targets = [logging.getLogger(name) for name in CHAIN_LOGGERS]
    for target in targets:
        target.addHandler(handler)
        target.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        for target in targets:
            target.removeHandler(handler)


# ---------------- unit level: the context survives the whole pipeline ----------------
class ContextRecordingExtraction:
    """Extraction service that reports the context it was called in."""

    api_key = "test-openrouter-key"

    def __init__(self, result: ExtractionResult):
        self.result = result
        self.seen_context: list[str | None] = []

    async def extract(self, text, session_id=None):
        self.seen_context.append(get_session_id())
        return self.result

    async def close(self):
        return None


async def test_the_session_stays_bound_until_the_pipeline_is_done():
    extraction = ContextRecordingExtraction(
        ExtractionResult(company_name="Ali", phone_e164="+77001234567")
    )
    container = FakeContainer(extraction=extraction, dedup=FakeDedup(match=None))
    storage, state = make_fsm()
    await state.set_state(LeadForm.Collecting)
    await state.set_data({"session_id": 1, "combined_text": "Ali Motors"})

    # Even with a stale value left over from an earlier update, the pipeline binds
    # its own session (production isolates updates per request task, and every entry
    # point rebinds explicitly).
    set_session_id("stale")
    try:
        await flow.finalize_collection(container, 1, 1, state)

        assert extraction.seen_context == ["1"], "the LLM call ran without a session_id"
        assert await state.get_state() == LeadForm.Reviewing.state
        assert get_session_id() == "stale", "the caller's context was not restored"
    finally:
        set_session_id(None)


async def test_a_previous_context_value_is_restored_afterwards():
    container = FakeContainer(
        extraction=FakeExtractionStub(), dedup=FakeDedup(match=None)
    )
    storage, state = make_fsm()
    await state.set_state(LeadForm.Collecting)
    await state.set_data({"session_id": 1, "combined_text": "Ali"})

    set_session_id("outer")
    try:
        await flow.finalize_collection(container, 1, 1, state)
        assert get_session_id() == "outer", "the pipeline reset the caller's context"
    finally:
        set_session_id(None)


class FakeExtractionStub:
    api_key = "test"

    async def extract(self, text, session_id=None):
        return ExtractionResult(company_name="Ali", phone_e164="+77001234567")


# ---------------- the whole chain through the real dispatcher ----------------
async def test_one_session_id_spans_llm_dedup_and_the_sheets_task(harness, captured_sessions):
    harness.extraction._results = [full_result()]

    # Lead #1: message → LLM → create → background sheet sync.
    await harness.send_text("ТОО Ромашка, Алматы, +7 700 123 45 67")
    await harness.send_command("/done")
    await harness.tap("add")
    assert await wait_until(lambda: len(harness.sheets.appends) == 1)

    # Lead #2: same company again → strong match → dedup decision → merge → sheet update.
    harness.extraction._results = [full_result()]
    await harness.send_text("ТОО Ромашка, Алматы, +7 700 123 45 67")
    await harness.send_command("/done")
    assert await wait_until(lambda: len(harness.sheets.updates) == 1)

    session_ids = [str(session.id) for session in await harness.sessions()]
    assert len(session_ids) == 2

    unlabelled = [
        (record.name, record.getMessage())
        for record, sid in captured_sessions.entries
        if sid is None
    ]
    assert unlabelled == [], f"log lines without a session_id: {unlabelled}"

    assert set(sid for _, sid in captured_sessions.entries) <= set(session_ids)

    # The parts the audit called out are all attributed, and attributed correctly.
    dedup_entries = [sid for record, sid in captured_sessions.entries if "dedup decision" in record.getMessage()]
    assert dedup_entries, "the dedup decision was not logged at all"
    assert dedup_entries == [session_ids[1]], "the dedup log lost its session_id"

    sheet_entries = captured_sessions.messages("sheets sync ok")
    assert len(sheet_entries) == 2
    assert sheet_entries == session_ids, "the background sync task lost its session_id"

    lead_entries = captured_sessions.messages("lead created") + captured_sessions.messages("lead merged")
    assert lead_entries == session_ids


async def test_the_sheets_task_inherits_the_context_of_the_user_action(harness, captured_sessions):
    """«Добавить» happens in a *new* update — it must rebind the same session."""
    harness.extraction._results = [full_result()]
    await harness.send_text("ТОО Ромашка, Алматы, +7 700 123 45 67")
    await harness.send_command("/done")

    await harness.tap("add")
    assert await wait_until(lambda: len(harness.sheets.appends) == 1)

    session_ids = [str(session.id) for session in await harness.sessions()]
    assert captured_sessions.messages("sheets sync ok") == session_ids
    assert captured_sessions.messages("lead created") == session_ids
