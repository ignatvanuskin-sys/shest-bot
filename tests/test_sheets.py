"""Google Sheets sync tests (escape + row mapping + append/update/retry via mocks)."""
from __future__ import annotations

from datetime import datetime

import pytest

from app import models
from app.services import sheets as sheets_mod
from app.services.sheets import (
    SheetsSyncService,
    escape_sheet_value,
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

    async def fake_update(row, values):
        calls.append((row, values))

    monkeypatch.setattr(svc, "_update", fake_update)
    row = await svc.sync_lead(lead)
    assert row == 7
    assert calls and calls[0][0] == 7


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
