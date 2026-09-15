"""FIX-4 regression: /undo works as a chain and is mirrored into the sheet.

Two production defects:

1. ``undo_last`` always picked the latest ``created``/``merged`` audit row, even if it
   had already been reversed — the second /undo answered «нечего откатывать» and an
   earlier lead could never be undone.
2. the undo never touched the table: reversing a creation left the row in Sheets, and
   reversing a merge left the merged values there (DB ↔ table drift).

The contract under test:

* several undos in a row walk back through the actions (LIFO);
* reversing a creation blanks the lead's 27 cells via the existing update backend and
  keeps the line (no renumbering, no Apps Script redeploy);
* reversing a merge rewrites the row from the audit snapshot *and leaves the merged
  duplicate archived* (FIX-21) — no living lead without a row, no extra table line
  on the next /resync;
* a failing sheet write does not roll the undo back — it is reported instead;
* the ``is_syncable`` guard for dead rows is untouched (that is a separate path).
"""
from __future__ import annotations

import json

from sqlalchemy import select

from app.models import AuditLog, Lead
from app.schemas.extraction import ExtractionResult
from app.services.lead_service import LeadService
from app.services.sheets import COLUMN_COUNT, escape_sheet_value, is_syncable
from tests.integration_harness import (
    RecordingSheets,
    harness,  # noqa: F401  — imported fixture
    no_network,  # noqa: F401  — autouse imported fixture (blocks non-loopback sockets)
    wait_until,
)

BLANK_ROW = [""] * COLUMN_COUNT


class NoSheetRow(RecordingSheets):
    """A sheets backend that cannot write the row (simulated failure)."""

    async def sync_lead(self, lead):
        return None

    async def clear_row(self, row, lead_id=None):
        return False


# ---------------- (a) the undo chain ----------------
async def test_undo_walks_back_through_several_creations(session_factory):
    svc = LeadService(session_factory)
    leads = [
        await svc.add_lead(1, ExtractionResult(company_name=name))
        for name in ("Первый", "Второй", "Третий")
    ]

    assert await svc.undo_last(1) == "Отменено создание лида #3"
    assert await svc.undo_last(1) == "Отменено создание лида #2"
    assert await svc.undo_last(1) == "Отменено создание лида #1"
    assert await svc.undo_last(1) is None, "the chain must end, not repeat itself"

    for lead in leads:
        assert (await svc.get_lead(lead.id)).deleted_at is not None


async def test_undo_chain_restores_a_merge_then_the_creation(session_factory):
    svc = LeadService(session_factory)
    existing = await svc.add_lead(
        1, ExtractionResult(company_name="Ali", phone_e164="+77001234567")
    )
    incoming = ExtractionResult(
        company_name="Ali", phone_e164="+77001234567", website="https://ali.kz"
    )
    merged = await svc.add_lead(1, incoming, merge_target_id=existing.id)

    assert await svc.undo_last(1) == "Отменено объединение лида #1"
    assert (await svc.get_lead(existing.id)).website is None  # snapshot restored
    # FIX-21: the duplicate stays archived — resurrecting it created a *living* lead
    # without a sheet row, and the next /resync appended a second line for a company
    # that is already in the table.
    duplicate = await svc.get_lead(merged.id)
    assert duplicate.deleted_at is not None, "the merged duplicate came back to life"
    assert duplicate.duplicate_of_id == existing.id

    # The merge is reversed now, so the next /undo reaches the creation behind it.
    assert await svc.undo_last(1) == "Отменено создание лида #1"
    assert (await svc.get_lead(existing.id)).deleted_at is not None
    assert await svc.undo_last(1) is None


async def test_undo_merge_leaves_no_living_lead_without_a_sheet_row(session_factory):
    """FIX-21: after the undo, every live lead owns a sheet row (or is queued)."""
    svc = LeadService(session_factory)
    existing = await svc.add_lead(
        1, ExtractionResult(company_name="Ali", phone_e164="+77001234567")
    )
    await svc.add_lead(
        1,
        ExtractionResult(company_name="Ali", phone_e164="+77001234567", city="Караганда"),
        merge_target_id=existing.id,
    )

    await svc.undo_last(1)

    live = [
        lead
        for lead in (await svc.get_last_leads(1, limit=50))
        if lead.deleted_at is None and lead.duplicate_of_id is None
    ]
    assert [lead.id for lead in live] == [existing.id]
    # The un-merged duplicate is *not* in the resync queue: it is history, and a
    # resync would add a spare line for a company that is already in the table.
    assert [lead.id for lead in await svc.get_unsynced_leads_all_owners()] == [existing.id]


async def test_undo_merge_records_that_the_duplicate_stayed_archived(session_factory):
    """The audit trail must say what happened to the duplicate (ТЗ §7)."""
    svc = LeadService(session_factory)
    existing = await svc.add_lead(
        1, ExtractionResult(company_name="Ali", phone_e164="+77001234567")
    )
    merged = await svc.add_lead(
        1,
        ExtractionResult(company_name="Ali", phone_e164="+77001234567", website="https://ali.kz"),
        merge_target_id=existing.id,
    )

    await svc.undo_last(1)

    async with session_factory() as session:
        result = await session.execute(
            select(AuditLog).where(AuditLog.action == "restored").order_by(AuditLog.id)
        )
        entries = list(result.scalars().all())
    assert len(entries) == 1
    details = json.loads(entries[0].details)
    assert entries[0].actor_id == 1 and entries[0].lead_id == existing.id
    assert details["from"] == "merged"
    assert details["duplicate_lead_id"] == merged.id
    assert details["duplicate_state"] == "archived"


async def test_undo_only_touches_the_actors_own_actions(session_factory):
    svc = LeadService(session_factory)
    mine = await svc.add_lead(1, ExtractionResult(company_name="Мой"))
    theirs = await svc.add_lead(2, ExtractionResult(company_name="Чужой"))

    assert await svc.undo_last(1) == f"Отменено создание лида #{mine.id}"

    assert (await svc.get_lead(theirs.id)).deleted_at is None
    assert await svc.undo_last(1) is None


# ---------------- (b) the undo reaches the sheet ----------------
async def test_undo_creation_blanks_the_row_and_keeps_it(session_factory):
    sheets = RecordingSheets(row=7)
    svc = LeadService(session_factory)
    lead = await svc.add_lead(1, ExtractionResult(company_name="Ali"))
    await svc.update_lead(lead.id, sheet_row=7)

    result = await svc.undo_last(1, sheets=sheets)

    assert result == "Отменено создание лида #1"
    assert sheets.updates == [(7, BLANK_ROW)], "the row was not cleared through _update"
    assert sheets.appends == [], "clearing must never append a new line"
    assert (await svc.get_lead(lead.id)).sheet_row == 7, "the row number must survive"


async def test_undo_merge_rewrites_the_row_from_the_snapshot(session_factory):
    sheets = RecordingSheets(row=7)
    svc = LeadService(session_factory)
    existing = await svc.add_lead(
        1, ExtractionResult(company_name="Ali", phone_e164="+77001234567")
    )
    await svc.update_lead(existing.id, sheet_row=7)

    incoming = ExtractionResult(
        company_name="Ali",
        phone_e164="+77001234567",
        city="Караганда",
        website="https://ali.kz",
    )
    duplicate = await svc.add_lead(1, incoming, merge_target_id=existing.id)

    result = await svc.undo_last(1, sheets=sheets)

    assert result == "Отменено объединение лида #1"
    assert sheets.appends == [], "rewriting the target must never append"
    row, values = sheets.updates[-1]
    assert row == 7
    assert values[2] == "Ali"
    assert values[11] == "", "the merged website is still in the table"
    assert values[4] == "", "the merged city is still in the table"
    assert values[6] == escape_sheet_value("+77001234567")
    # FIX-21: the un-merged duplicate stays history — it is not synced and owns no row.
    assert [lead.id for lead in sheets.synced] == [existing.id]
    assert (await svc.get_lead(duplicate.id)).sheet_row is None


async def test_undo_merge_of_an_unsynced_target_appends_its_row(session_factory):
    """The un-merged lead must end up in the table — its row is brought in line."""
    sheets = RecordingSheets(row=7)
    svc = LeadService(session_factory)
    existing = await svc.add_lead(
        1, ExtractionResult(company_name="Ali", phone_e164="+77001234567")
    )
    duplicate = await svc.add_lead(
        1,
        ExtractionResult(company_name="Ali", phone_e164="+77001234567", city="Караганда"),
        merge_target_id=existing.id,
    )

    result = await svc.undo_last(1, sheets=sheets)

    assert result == "Отменено объединение лида #1"
    assert len(sheets.appends) == 1, "the un-merged lead must get its line"
    assert sheets.appends[0][0] == str(existing.id), "the wrong lead was written"
    assert sheets.updates == [], "an unsynced target has no row to update"
    assert (await svc.get_lead(existing.id)).sheet_row == 7
    assert (await svc.get_lead(duplicate.id)).sheet_row is None


async def test_undo_without_a_sheet_row_does_not_fail(session_factory):
    """A lead that never reached the sheet is undone without any sheet call."""
    sheets = RecordingSheets(row=7)
    svc = LeadService(session_factory)
    await svc.add_lead(1, ExtractionResult(company_name="Ali"))

    assert await svc.undo_last(1, sheets=sheets) == "Отменено создание лида #1"
    assert sheets.updates == [] and sheets.appends == []


async def test_sheet_failure_is_reported_without_losing_the_undo(session_factory):
    sheets = NoSheetRow()
    svc = LeadService(session_factory)
    lead = await svc.add_lead(1, ExtractionResult(company_name="Ali"))
    await svc.update_lead(lead.id, sheet_row=7)

    result = await svc.undo_last(1, sheets=sheets)

    assert result is not None and result.startswith("Отменено создание лида #1")
    assert "очистить не удалось" in result
    assert (await svc.get_lead(lead.id)).deleted_at is not None, "the undo was rolled back"


async def test_clearing_a_row_does_not_resurrect_the_syncable_guard(session_factory):
    """``clear_row`` is an explicit path; the dead-row guard of ``sync_lead`` stays."""
    sheets = RecordingSheets(row=7)
    svc = LeadService(session_factory)
    lead = await svc.add_lead(1, ExtractionResult(company_name="Ali"))
    await svc.update_lead(lead.id, sheet_row=7)

    await svc.undo_last(1, sheets=sheets)

    dead = await svc.get_lead(lead.id)
    assert is_syncable(dead) is False, "a deleted lead must not be syncable again"
    assert sheets.synced == [], "sync_lead must never see the dead row"
    assert await sheets.sync_lead(dead) is None, "dead rows stay refused"


# ---------------- through the real dispatcher ----------------
async def test_undo_command_clears_the_sheet_row_and_then_reports_nothing_left(harness):
    harness.extraction._results = [ExtractionResult(company_name="Ромашка", city="Алматы")]
    await harness.send_text("ТОО Ромашка, Алматы")
    await harness.send_command("/done")
    await harness.tap("add")
    # /undo blanks the *cached* row, so wait for the row number to be committed
    # (an append alone is visible earlier than that write).
    row = await harness.wait_for_sheet_row(1)
    assert harness.sheets.appends and len(harness.sheets.appends) == 1

    await harness.send_command("/undo")

    assert harness.bot.contains("Отменено создание лида #1")
    assert harness.sheets.updates == [(row, [""] * 27)]
    assert harness.sheets.appends == [(harness.sheets.appends[0])], "no new line was written"
    assert (await harness.leads())[0].deleted_at is not None

    await harness.send_command("/undo")
    assert harness.bot.contains("Нечего откатывать")


async def test_two_leads_can_be_undone_one_after_another_via_the_command(harness):
    for index, name in enumerate(("Первая", "Вторая"), start=1):
        harness.extraction._results = [ExtractionResult(company_name=name, city="Алматы")]
        await harness.send_text(f"ТОО {name}, Алматы")
        await harness.send_command("/done")
        await harness.tap("add")
        assert await harness.wait_for_sheet_row(index)

    await harness.send_command("/undo")
    assert harness.bot.contains("Отменено создание лида #2")
    await harness.send_command("/undo")
    assert harness.bot.contains("Отменено создание лида #1")

    leads = await harness.leads()
    assert [lead.id for lead in leads] == [1, 2]
    assert all(lead.deleted_at is not None for lead in leads)
    # Both rows were blanked, in the sheet order they were appended in.
    assert [row for row, _ in harness.sheets.updates] == [harness.sheets.row] * 2


async def test_undo_merge_restores_the_row_through_the_command(harness):
    first = ExtractionResult(
        company_name="Alimotors",
        city="Караганда",
        phone_raw="+7 700 123 45 67",
        phone_e164="+77001234567",
    )
    harness.extraction._results = [first]
    await harness.send_text("ТОО Alimotors, Караганда, +7 700 123 45 67")
    await harness.send_command("/done")
    await harness.tap("add")
    row = await harness.wait_for_sheet_row(1)
    assert len(harness.sheets.appends) == 1

    # Same phone written differently → strong match → silent auto-merge into #1.
    harness.extraction._results = [
        ExtractionResult(
            company_name="Алимоторс",
            city="Караганда",
            phone_raw="8 700 123 45 67",
            phone_e164="+77001234567",
            website="https://alimotors.kz",
        )
    ]
    await harness.send_text("Алимоторс, Караганда, 8 700 123 45 67")
    await harness.send_command("/done")
    assert await wait_until(lambda: len(harness.sheets.updates) == 1)
    assert harness.sheets.updates[0][1][11] == "https://alimotors.kz", "merge is not in the row"

    await harness.send_command("/undo")

    assert harness.bot.contains("Отменено объединение лида #1")
    final_row, values = harness.sheets.updates[-1]
    assert final_row == row
    assert values[11] == "", "the merged website is still in the table"
    assert values[2] == "Alimotors"
    assert harness.sheets.appends == [harness.sheets.appends[0]], "no extra line was appended"

    # FIX-21: the duplicate is archived, so the resync queue stays empty and the
    # table does not grow a second line for the company that is already there.
    leads = {lead.id: lead for lead in await harness.leads()}
    assert leads[1].website is None, "the target must reflect the un-merge"
    assert leads[2].deleted_at is not None and leads[2].duplicate_of_id == 1
    appends_before = list(harness.sheets.appends)

    await harness.send_command("/resync")

    assert harness.bot.contains("Нет лидов, ожидающих синхронизации.")
    assert harness.sheets.appends == appends_before, "a spare line was appended after /undo"
    assert (await harness.leads())[1].sheet_row is None
