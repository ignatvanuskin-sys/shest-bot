"""Google Sheets sync tests (escape + row mapping + append/update/retry via mocks)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from app import models
from app.services import sheets as sheets_mod
from app.services.sheets import (
    SheetsSyncService,
    escape_sheet_value,
    is_syncable,
    lead_to_row_values,
)


def test_escape_formula_prefixes():
    assert escape_sheet_value("=SUM(A1)") == "'=SUM(A1)"
    assert escape_sheet_value("+123") == "'+123"
    assert escape_sheet_value("-2+3") == "'-2+3"
    assert escape_sheet_value("@user") == "'@user"
    assert escape_sheet_value("normal text") == "normal text"
    assert escape_sheet_value(None) == ""
    assert escape_sheet_value(123) == "123"


def test_lead_to_row_values_27_columns_and_escaping():
    lead = models.Lead(
        id=1,
        created_at=datetime(2026, 9, 14, 12, 0, 0),
        company_name="=Ali Motors",
        phone="+77001234567",
        services='["ремонт", "ходовая"]',
        tags='["авто"]',
        rating=4.5,
        reviews_count=3,
        needs_review=True,
    )
    values = lead_to_row_values(lead)
    assert len(values) == 27
    assert values[0] == "1"  # A: ID
    assert values[2] == "'=Ali Motors"  # C: company (escaped)
    assert values[6] == "'+77001234567"  # G: phone (escaped)
    assert values[14] == "ремонт, ходовая"  # O: services joined
    assert values[15] == "авто"  # P: tags joined
    assert values[17] == "4.5"  # R: rating
    assert values[18] == "3"  # S: reviews count
    assert values[26] == "⚠️"  # AA: needs_review


@pytest.mark.asyncio
async def test_sync_skips_when_not_configured():
    svc = SheetsSyncService("", "")
    lead = models.Lead(id=1)
    assert await svc.sync_lead(lead) is None


@pytest.mark.asyncio
async def test_sync_append_sets_row(monkeypatch):
    svc = SheetsSyncService("sheetid", "e30=")  # base64 of "{}"
    lead = models.Lead(id=1)

    async def fake_append(values):
        return 5

    monkeypatch.setattr(svc, "_append", fake_append)
    row = await svc.sync_lead(lead)
    assert row == 5
    assert lead.sheet_row == 5


@pytest.mark.asyncio
async def test_sync_update_uses_cached_row(monkeypatch):
    svc = SheetsSyncService("sheetid", "e30=")
    lead = models.Lead(id=1, sheet_row=7)
    calls = []

    async def fake_update(row, values, lead_id=None):
        calls.append((row, values, lead_id))

    monkeypatch.setattr(svc, "_update", fake_update)
    row = await svc.sync_lead(lead)
    assert row == 7
    assert [call[0] for call in calls] == [7]
    # FIX-7: the backend must receive the lead identity, not just a row number.
    assert calls[0][2] == 1, "the update must carry the lead id for the ownership check"


@pytest.mark.asyncio
async def test_sync_update_follows_a_moved_row_and_repairs_the_cache(monkeypatch):
    """A backend that re-targeted the row reports it back → the cached number heals."""
    svc = SheetsSyncService("sheetid", "e30=")
    lead = models.Lead(id=1, sheet_row=7)

    async def fake_update(row, values, lead_id=None):
        return 12  # a line was inserted above: the lead now lives on row 12

    monkeypatch.setattr(svc, "_update", fake_update)
    row = await svc.sync_lead(lead)

    assert row == 12
    assert lead.sheet_row == 12, "the stale row number must not stay in the cache"


@pytest.mark.asyncio
async def test_clear_row_blanks_27_cells_and_keeps_the_line(monkeypatch):
    """``clear_row`` is the /undo path: an explicit update, never ``is_syncable``."""
    svc = SheetsSyncService("sheetid", "e30=")
    calls = []

    async def fake_update(row, values, lead_id=None):
        calls.append((row, values))

    monkeypatch.setattr(svc, "_update", fake_update)

    assert await svc.clear_row(7) is True
    assert calls == [(7, [""] * 27)]


@pytest.mark.asyncio
async def test_clear_row_reports_failure_after_retries(monkeypatch):
    monkeypatch.setattr(sheets_mod, "RETRY_BASE_DELAY", 0.0)
    monkeypatch.setattr(sheets_mod, "RETRY_MAX", 3)
    svc = SheetsSyncService("sheetid", "e30=")
    attempts = {"n": 0}

    async def flaky(row, values, lead_id=None):
        attempts["n"] += 1
        raise RuntimeError("boom")

    monkeypatch.setattr(svc, "_update", flaky)

    assert await svc.clear_row(7) is False
    assert attempts["n"] == 3


@pytest.mark.asyncio
async def test_clear_row_skips_when_not_configured():
    svc = SheetsSyncService("", "")
    assert await svc.clear_row(7) is False


@pytest.mark.asyncio
async def test_sync_retries_then_gives_up(monkeypatch):
    monkeypatch.setattr(sheets_mod, "RETRY_BASE_DELAY", 0.0)
    monkeypatch.setattr(sheets_mod, "RETRY_MAX", 3)
    svc = SheetsSyncService("sheetid", "e30=")
    lead = models.Lead(id=1)
    attempts = {"n": 0}

    async def flaky(values):
        attempts["n"] += 1
        raise RuntimeError("boom")

    monkeypatch.setattr(svc, "_append", flaky)
    row = await svc.sync_lead(lead)
    assert row is None
    assert attempts["n"] == 3
    assert lead.sheet_row is None


# ---------------- merged/deleted rows never reach the sheet ----------------
#
# Regression: after a dedup merge the bot synced the *duplicate* row. That row has no
# sheet_row, so the sync appended a second line for a company already in the table.
# The service now refuses such rows outright, whatever the caller passes in.

_DEAD_ROW_SHAPES = {
    "merged": {"duplicate_of_id": 1, "deleted_at": datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)},
    "deleted": {"deleted_at": datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)},
    "merged_without_timestamp": {"duplicate_of_id": 1},
}


def test_is_syncable_only_for_live_rows():
    assert is_syncable(models.Lead(id=1)) is True
    assert is_syncable(models.Lead(id=2, **(_DEAD_ROW_SHAPES["merged"]))) is False
    assert is_syncable(models.Lead(id=3, **(_DEAD_ROW_SHAPES["deleted"]))) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", sorted(_DEAD_ROW_SHAPES))
async def test_sync_refuses_merged_or_deleted_rows(shape, monkeypatch, caplog):
    svc = SheetsSyncService("sheetid", "e30=")
    lead = models.Lead(id=2, **_DEAD_ROW_SHAPES[shape])
    appends: list = []
    updates: list = []

    async def fake_append(values):
        appends.append(values)
        return 5

    async def fake_update(row, values, lead_id=None):
        updates.append((row, values))

    monkeypatch.setattr(svc, "_append", fake_append)
    monkeypatch.setattr(svc, "_update", fake_update)

    with caplog.at_level(logging.WARNING, logger="app.services.sheets"):
        row = await svc.sync_lead(lead)

    assert row is None, "a refused sync must report failure, not a row"
    assert appends == [], "a dead row must never be appended"
    assert updates == [], "a dead row must never overwrite a live row"
    assert lead.sheet_row is None

    refusals = [r for r in caplog.records if getattr(r, "action", None) == "sheets_sync_refused"]
    assert refusals, "the refusal must be logged (action=sheets_sync_refused)"
    assert "merged or deleted" in refusals[0].getMessage()
    assert refusals[0].lead_id == 2
