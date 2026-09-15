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
* reversing a merge rewrites the row from the audit snapshot;
* a failing sheet write does not roll the undo back — it is reported instead;
* the ``is_syncable`` guard for dead rows is untouched (that is a separate path).
"""
from __future__ import annotations

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
    assert (await svc.get_lead(merged.id)).deleted_at is None

    # The merge is reversed now, so the next /undo reaches the creation behind it.
    assert await svc.undo_last(1) == "Отменено создание лида #1"
    assert (await svc.get_lead(existing.id)).deleted_at is not None
    assert await svc.undo_last(1) is None


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
    await svc.add_lead(1, incoming, merge_target_id=existing.id)

    result = await svc.undo_last(1, sheets=sheets)

    assert result == "Отменено объединение лида #1"
    assert sheets.appends == []
    row, values = sheets.updates[-1]
    assert row == 7
    assert values[2] == "Ali"
    assert values[11] == "", "the merged website is still in the table"
    assert values[4] == "", "the merged city is still in the table"
    assert values[6] == escape_sheet_value("+77001234567")


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
    assert await wait_until(lambda: len(harness.sheets.appends) == 1)
    row = harness.sheets.row
    assert (await harness.leads())[0].sheet_row == row

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
        assert await wait_until(lambda count=index: len(harness.sheets.appends) == count)

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
    assert await wait_until(lambda: len(harness.sheets.appends) == 1)
    row = harness.sheets.row

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
