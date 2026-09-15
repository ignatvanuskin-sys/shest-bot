"""FIX-25a: ``lead_sessions.status`` follows the dialog instead of standing still.

The column is what the database says about a user's dialog when nobody is watching
the process, and it feeds the startup reconciliation (an interrupted dialog must be
closable). Two things were wrong: «editing» — the status the ТЗ names — was never
written, and the card was drawn after a correction while the row still claimed the
session was in its previous stage.

The vocabulary under test (documented in README):

    collecting → review → editing → review → done
                       ↘ cancelled (cancel/restart)
"""
from __future__ import annotations

from app.bot.keyboards import CB_ADD, CB_EDIT, CB_EDIT_DONE, CB_FIELD_PREFIX
from app.bot.states import LeadForm
from app.main import startup_runtime
from app.schemas.extraction import ExtractionResult
from app.services.lead_service import (
    SESSION_STATUS_CANCELLED,
    SESSION_STATUS_COLLECTING,
    SESSION_STATUS_DONE,
    SESSION_STATUS_EDITING,
    SESSION_STATUS_REVIEW,
    SESSION_STATUSES,
    UNFINISHED_SESSION_STATUSES,
)
from tests.integration_harness import (
    harness,  # noqa: F401  — imported fixture
    no_network,  # noqa: F401  — autouse imported fixture (blocks non-loopback sockets)
)


def full_result() -> ExtractionResult:
    return ExtractionResult(
        company_name="Ромашка",
        city="Алматы",
        phone_raw="+7 700 123 45 67",
        phone_e164="+77001234567",
        source_guess="2gis",
    )


async def _only_session(harness):
    sessions = await harness.sessions()
    assert len(sessions) == 1, f"expected one session, got {len(sessions)}"
    return sessions[0]


# ---------------- the vocabulary itself ----------------
def test_the_status_vocabulary_is_the_documented_one():
    assert set(SESSION_STATUSES) == {
        "collecting",
        "review",
        "editing",
        "done",
        "cancelled",
    }
    # Every *open* status must be closable by the startup reconciliation, otherwise a
    # dialog interrupted by a restart would stay open for ever (FIX-9).
    assert set(UNFINISHED_SESSION_STATUSES) == {
        SESSION_STATUS_COLLECTING,
        SESSION_STATUS_REVIEW,
        SESSION_STATUS_EDITING,
    }
    assert SESSION_STATUS_DONE not in UNFINISHED_SESSION_STATUSES
    assert SESSION_STATUS_CANCELLED not in UNFINISHED_SESSION_STATUSES


# ---------------- the statuses are actually written ----------------
async def test_collecting_then_review_then_done(harness):
    harness.extraction._results = [full_result()]

    await harness.send_text("ТОО Ромашка, Алматы, +7 700 123 45 67")
    assert (await _only_session(harness)).status == SESSION_STATUS_COLLECTING

    await harness.send_command("/done")
    assert (await _only_session(harness)).status == SESSION_STATUS_REVIEW

    await harness.tap(CB_ADD)
    session = await _only_session(harness)
    assert session.status == SESSION_STATUS_DONE
    assert session.resulting_lead_id is not None


async def test_cancel_marks_the_session_cancelled(harness):
    await harness.send_text("ТОО Ромашка, Алматы")

    await harness.send_command("/cancel")

    assert (await _only_session(harness)).status == SESSION_STATUS_CANCELLED


async def test_opening_the_field_picker_marks_the_session_editing(harness):
    harness.extraction._results = [full_result()]
    await harness.send_text("ТОО Ромашка, Алматы, +7 700 123 45 67")
    await harness.send_command("/done")

    await harness.tap(CB_EDIT)

    assert await harness.fsm_state() == LeadForm.EditingField.state
    assert (await _only_session(harness)).status == SESSION_STATUS_EDITING


async def test_applying_a_correction_returns_the_session_to_review(harness):
    harness.extraction._results = [full_result()]
    await harness.send_text("ТОО Ромашка, Алматы, +7 700 123 45 67")
    await harness.send_command("/done")
    await harness.tap(CB_EDIT)

    await harness.tap(f"{CB_FIELD_PREFIX}city")
    await harness.send_text("Караганда")

    assert await harness.fsm_state() == LeadForm.Reviewing.state
    assert (await _only_session(harness)).status == SESSION_STATUS_REVIEW, (
        "the row still claims «editing» after the card came back"
    )
    assert "Караганда" in (harness.bot.last_message().text or "")


async def test_leaving_the_field_picker_without_a_change_returns_to_review(harness):
    harness.extraction._results = [full_result()]
    await harness.send_text("ТОО Ромашка, Алматы, +7 700 123 45 67")
    await harness.send_command("/done")
    await harness.tap(CB_EDIT)
    assert (await _only_session(harness)).status == SESSION_STATUS_EDITING

    await harness.tap(CB_EDIT_DONE)

    assert (await _only_session(harness)).status == SESSION_STATUS_REVIEW


async def test_a_session_left_in_editing_is_closed_by_the_next_start(harness):
    """«editing» is an *open* status: a restart must be able to close it (FIX-9)."""
    harness.extraction._results = [full_result()]
    await harness.send_text("ТОО Ромашка, Алматы, +7 700 123 45 67")
    await harness.send_command("/done")
    await harness.tap(CB_EDIT)
    assert (await _only_session(harness)).status == SESSION_STATUS_EDITING

    await startup_runtime(harness.container)

    assert (await _only_session(harness)).status == SESSION_STATUS_CANCELLED


async def test_the_done_command_does_not_touch_a_saved_session(harness):
    """A finished dialog keeps «done» even if the buffer timer fires again."""
    harness.extraction._results = [full_result()]
    await harness.send_text("ТОО Ромашка, Алматы, +7 700 123 45 67")
    await harness.send_command("/done")
    await harness.tap(CB_ADD)
    assert await harness.wait_for_sheet_row(1)

    await harness.send_command("/done")  # no collecting state → nothing to do

    assert (await _only_session(harness)).status == SESSION_STATUS_DONE
    assert await harness.lead_count() == 1, "a re-run must not save a second lead"
