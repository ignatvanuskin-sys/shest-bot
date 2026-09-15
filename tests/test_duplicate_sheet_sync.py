"""Regression: a dedup merge must never add a second row to the Google Sheet.

Reproduced bug: the strong-match branch synced the row returned by
``LeadService.add_lead(merge_target_id=...)`` — the *duplicate* row, which is
``duplicate_of_id``/``deleted_at`` and has no ``sheet_row``. Sheets therefore
appended a second line for a company that was already in the table, and the real
lead's row kept its pre-merge values (missing the merged phone/instagram/whatsapp).

These tests drive the real dispatcher (``tests.integration_harness``) so the whole
chain is exercised: FSM → extraction → dedup → merge → sheets, with the recording
sheets backend distinguishing appends from updates.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.bot import flow
from app.bot.keyboards import CB_ADD, CB_DUP_CONTACT, CB_DUP_SAME
from app.schemas.extraction import ExtractionResult
from app.services.sheets import escape_sheet_value
from tests.integration_harness import (
    harness,  # noqa: F401  — imported fixture
    no_network,  # noqa: F401  — autouse imported fixture (blocks non-loopback sockets)
    wait_until,
)

# The exact pair from the live run: one company, the phone written two ways
# ("+7 700 123 45 67" and "8 700 123 45 67"). Normalization is *not* at fault — both
# extractions already carry the same phone_e164/instagram and classify_match() calls
# the pair "strong" on the phone.
FIRST = ExtractionResult(
    company_name="Alimotors",
    city="Караганда",
    phone_raw="+7 700 123 45 67",
    phone_e164="+77001234567",
    instagram="alimotors",
)
SECOND = ExtractionResult(
    company_name="Алимоторс",  # latin → cyrillic spelling of the same name
    city="Караганда",
    phone_raw="8 700 123 45 67",
    phone_e164="+77001234567",
    instagram="alimotors",
    whatsapp_number="8 700 123 45 67",  # only the second message has it
)


def test_merge_sync_target_resolves_to_the_live_row():
    dead = SimpleNamespace(id=2, duplicate_of_id=1)
    live = SimpleNamespace(id=5, duplicate_of_id=None)
    assert flow.merge_sync_target(dead) == 1, "a merged duplicate is synced via its target"
    assert flow.merge_sync_target(live) == 5, "a plain lead is synced as itself"


async def _add_first_lead(harness) -> int:
    """Save the first lead and return its id (the sheet row is cached)."""
    harness.extraction._results = [FIRST]
    await harness.send_text("ТОО Alimotors, Караганда, +7 700 123 45 67")
    await harness.send_command("/done")
    await harness.tap(CB_ADD)

    assert await wait_until(lambda: len(harness.sheets.appends) == 1)
    leads = await harness.leads()
    assert len(leads) == 1
    assert leads[0].sheet_row == harness.sheets.row, "the row must be cached → next sync updates"
    return leads[0].id


async def test_strong_duplicate_merge_updates_the_lead_row_and_appends_nothing(harness):
    first_id = await _add_first_lead(harness)
    first_row = harness.sheets.row

    # Same company, phone in the other format → strong match → silent auto-merge.
    harness.extraction._results = [SECOND]
    await harness.send_text("Алимоторс, Караганда, 8 700 123 45 67")
    await harness.send_command("/done")

    assert await wait_until(lambda: len(harness.sheets.updates) == 1)
    assert await wait_until(lambda: harness.bot.contains(f"обновлён лид #{first_id}"))

    # Exactly what the task fixes: one row for the company, updated — never appended.
    assert len(harness.sheets.appends) == 1, "the merge appended a second row"
    assert len(harness.sheets.updates) == 1, "the live lead row was not refreshed"
    row, values = harness.sheets.updates[0]
    assert row == first_row == 7
    assert values[0] == str(first_id), "column A must be the live lead, not the duplicate"
    assert values[7] == escape_sheet_value("+77001234567"), "merged whatsapp missing from the row"
    assert values[9] == "alimotors"

    # Nothing dead ever reached the sheets backend.
    for synced in harness.sheets.synced:
        assert synced.duplicate_of_id is None and synced.deleted_at is None

    # And the merge itself is unchanged: second row is a dead duplicate of the first.
    leads = {lead.id: lead for lead in await harness.leads()}
    assert len(leads) == 2
    live = [lead for lead in leads.values() if lead.duplicate_of_id is None and lead.deleted_at is None]
    assert [lead.id for lead in live] == [first_id]
    assert leads[first_id].whatsapp_number == "+77001234567", "merged field must be stored"
    duplicate = next(lead for lead in leads.values() if lead.id != first_id)
    assert duplicate.duplicate_of_id == first_id
    assert duplicate.deleted_at is not None
    assert duplicate.sheet_row is None


@pytest.mark.parametrize(
    ("callback", "expected_phone"),
    [
        (CB_DUP_SAME, "+77001234567"),  # «Это тот же лид» — keep the existing contact
        (CB_DUP_CONTACT, "+77019998877"),  # «Объединить, но обновить контакт»
    ],
)
async def test_medium_duplicate_choice_syncs_the_live_target_only(harness, callback, expected_phone):
    first_id = await _add_first_lead(harness)
    first_row = harness.sheets.row

    # Same name + same city, no shared hard identifier → medium: the bot must ask.
    harness.extraction._results = [
        ExtractionResult(company_name="Alimotors", city="Караганда", phone_raw="+7 701 999 88 77")
    ]
    await harness.send_text("Alimotors, Караганда, +7 701 999 88 77")
    await harness.send_command("/done")
    assert harness.bot.contains("Похоже на уже существующий лид")
    assert len(harness.sheets.appends) == 1, "asking must not write to the sheet"

    await harness.tap(callback)

    assert await wait_until(lambda: len(harness.sheets.updates) == 1)
    assert len(harness.sheets.appends) == 1, "a merge must not append a second row"
    row, values = harness.sheets.updates[0]
    assert row == first_row
    assert values[0] == str(first_id), "column A must be the live lead, not the duplicate"
    assert values[6] == escape_sheet_value(expected_phone)

    leads = {lead.id: lead for lead in await harness.leads()}
    assert leads[first_id].phone == expected_phone
    duplicate = next(lead for lead in leads.values() if lead.id != first_id)
    assert duplicate.duplicate_of_id == first_id and duplicate.sheet_row is None


async def test_resync_never_pushes_the_dead_duplicate(harness):
    """The manual /resync path must not resurrect the merged row either."""
    first_id = await _add_first_lead(harness)

    harness.extraction._results = [SECOND]
    await harness.send_text("Алимоторс, Караганда, 8 700 123 45 67")
    await harness.send_command("/done")
    assert await wait_until(lambda: len(harness.sheets.updates) == 1)

    # The merged row is unsynced (sheet_row is None) — exactly the shape that used to
    # be treated as "needs a new row". It must not be picked up by /resync.
    dead = next(lead for lead in await harness.leads() if lead.duplicate_of_id == first_id)
    assert dead.sheet_row is None

    await harness.send_command("/resync")

    assert harness.bot.contains("Нет лидов, ожидающих синхронизации.")
    assert len(harness.sheets.appends) == 1
    assert len(harness.sheets.updates) == 1
    assert [lead.id for lead in harness.sheets.synced] == [first_id, first_id]
