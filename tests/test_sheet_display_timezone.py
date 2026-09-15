"""FIX-14: the table shows dates in the owner's timezone, the database keeps UTC.

The sheet used to print the raw UTC value of «Дата добавления» / «Дата последнего
контакта», so an owner in UTC+5 saw every row five hours in the past. Only the
*display* moved — :class:`Lead` still stores UTC, which these tests assert.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import models
from app.config import get_settings
from app.services.sheets import (
    _format_datetime,
    display_timezone,
    get_display_timezone,
    lead_to_row_values,
)

# Column indexes in the A..AA row.
CREATED_AT_COLUMN = 1  # B
LAST_CONTACT_COLUMN = 25  # Z


def test_default_display_timezone_is_almaty():
    assert get_settings().DISPLAY_TIMEZONE == "Asia/Almaty"
    offset = _format_datetime(datetime(2026, 9, 14, 12, 0, 0)).split(" ")[1]
    assert offset == "17:00:00", "UTC+5 must be applied by default"


def test_naive_utc_value_is_shifted_for_the_sheet():
    lead = models.Lead(
        id=1,
        created_at=datetime(2026, 9, 14, 12, 0, 0),
        last_contact_at=datetime(2026, 9, 14, 13, 30, 0),
    )

    values = lead_to_row_values(lead)

    assert values[CREATED_AT_COLUMN] == "2026-09-14 17:00:00"
    assert values[LAST_CONTACT_COLUMN] == "2026-09-14 18:30:00"


def test_timezone_aware_utc_value_is_shifted_the_same_way():
    lead = models.Lead(
        id=1, created_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)
    )

    assert lead_to_row_values(lead)[CREATED_AT_COLUMN] == "2026-09-14 17:00:00"


def test_explicit_timezone_argument_wins():
    lead = models.Lead(id=1, created_at=datetime(2026, 9, 14, 12, 0, 0))

    values = lead_to_row_values(lead, tz=timezone.utc)

    assert values[CREATED_AT_COLUMN] == "2026-09-14 12:00:00"


def test_missing_dates_stay_empty():
    assert lead_to_row_values(models.Lead(id=1))[CREATED_AT_COLUMN] == ""
    assert lead_to_row_values(models.Lead(id=1))[LAST_CONTACT_COLUMN] == ""
    assert _format_datetime(None) == ""


def test_configured_timezone_is_used(monkeypatch):
    monkeypatch.setenv("DISPLAY_TIMEZONE", "UTC")
    get_settings.cache_clear()
    try:
        lead = models.Lead(id=1, created_at=datetime(2026, 9, 14, 12, 0, 0))
        assert lead_to_row_values(lead)[CREATED_AT_COLUMN] == "2026-09-14 12:00:00"
    finally:
        get_settings.cache_clear()


def test_an_explicit_offset_string_is_accepted(monkeypatch):
    """No IANA database needed: «+05:00» must work as-is."""
    monkeypatch.setenv("DISPLAY_TIMEZONE", "+05:00")
    get_settings.cache_clear()
    try:
        lead = models.Lead(id=1, created_at=datetime(2026, 9, 14, 12, 0, 0))
        assert lead_to_row_values(lead)[CREATED_AT_COLUMN] == "2026-09-14 17:00:00"
    finally:
        get_settings.cache_clear()


def test_unknown_timezone_degrades_to_utc_instead_of_raising():
    """A typo in the variable must never break the sheet write."""
    assert display_timezone("Nowhere/Atlantis") == timezone.utc
    assert display_timezone(None) == display_timezone("Asia/Almaty")
    assert display_timezone("") == display_timezone("Asia/Almaty")


def test_zoneinfo_absence_falls_back_to_a_fixed_offset(monkeypatch):
    """Windows has no IANA database; Asia/Almaty must still be UTC+5."""
    import zoneinfo

    def boom(_key):  # pragma: no cover - exercised via monkeypatch below
        raise zoneinfo.ZoneInfoNotFoundError("no tz database")

    monkeypatch.setattr(zoneinfo, "ZoneInfo", boom)
    display_timezone.cache_clear()
    try:
        assert display_timezone("Asia/Almaty") == timezone(timedelta(hours=5))
    finally:
        display_timezone.cache_clear()


@pytest.mark.asyncio
async def test_the_database_keeps_utc(session_factory):
    """The shift is display-only: stored timestamps stay exactly as written."""
    svc_lead = models.Lead(owner_user_id=1, created_at=datetime(2026, 9, 14, 12, 0, 0))
    async with session_factory() as session:
        session.add(svc_lead)
        await session.commit()

    async with session_factory() as session:
        stored = await session.get(models.Lead, svc_lead.id)

    assert stored.created_at == datetime(2026, 9, 14, 12, 0, 0), "the DB value moved!"
    assert lead_to_row_values(stored)[CREATED_AT_COLUMN] == "2026-09-14 17:00:00"
