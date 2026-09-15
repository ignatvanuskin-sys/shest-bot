"""FIX-13 (source_url hygiene) + FIX-17 (literal /search) — data-level fixes."""
from __future__ import annotations

import pytest

from app.schemas.extraction import ExtractionResult
from app.services.lead_service import LeadService
from app.services.normalize import LIKE_ESCAPE, escape_like


# ---------------- FIX-13: source_url is cleaned like website ----------------
@pytest.mark.asyncio
async def test_source_url_loses_utm_parameters_on_save(session_factory):
    svc = LeadService(session_factory)
    lead = await svc.add_lead(
        1,
        ExtractionResult(
            company_name="Ali Motors",
            source_guess="2gis",
            source_url="https://2gis.kz/almaty/firm/123?utm_source=2gis&utm_medium=link",
        ),
    )

    stored = (await svc.get_lead(lead.id)).source_url
    assert stored == "https://2gis.kz/almaty/firm/123"
    assert "utm_source" not in stored


@pytest.mark.asyncio
async def test_source_url_loses_fbclid(session_factory):
    svc = LeadService(session_factory)
    lead = await svc.add_lead(
        1,
        ExtractionResult(
            company_name="Ali",
            source_url="https://www.instagram.com/alimotors/?fbclid=IwAR123&igshid=abc",
        ),
    )

    stored = (await svc.get_lead(lead.id)).source_url
    assert "fbclid" not in stored and "igshid" not in stored
    assert stored == "https://instagram.com/alimotors"


@pytest.mark.asyncio
async def test_source_url_keeps_meaningful_parameters(session_factory):
    """Only tracking noise is removed — a deep link must stay usable."""
    svc = LeadService(session_factory)
    lead = await svc.add_lead(
        1,
        ExtractionResult(
            company_name="Ali",
            source_url="https://2gis.kz/firm/123?firm=456&utm_campaign=autumn",
        ),
    )

    stored = (await svc.get_lead(lead.id)).source_url
    assert "firm=456" in stored
    assert "utm_campaign" not in stored


@pytest.mark.asyncio
async def test_missing_source_url_stays_none(session_factory):
    svc = LeadService(session_factory)
    lead = await svc.add_lead(1, ExtractionResult(company_name="Ali"))

    assert (await svc.get_lead(lead.id)).source_url is None


# ---------------- FIX-17: LIKE wildcards are matched literally ----------------
def test_escape_like_escapes_every_wildcard_and_the_escape_char():
    assert escape_like("100%") == "100\\%"
    assert escape_like("a_b") == "a\\_b"
    assert escape_like("C:\\temp") == "C:\\\\temp"
    assert escape_like("plain") == "plain"
    assert LIKE_ESCAPE == "\\"


@pytest.mark.asyncio
async def test_search_treats_percent_literally(session_factory):
    """A «%» in the query used to return the whole base (wildcard match)."""
    svc = LeadService(session_factory)
    hit = await svc.add_lead(1, ExtractionResult(company_name="Скидка 100% сегодня"))
    await svc.add_lead(1, ExtractionResult(company_name="Обычная компания"))

    found = await svc.search_leads(1, "100%")

    assert [lead.id for lead in found] == [hit.id]


@pytest.mark.asyncio
async def test_search_treats_underscore_literally(session_factory):
    svc = LeadService(session_factory)
    hit = await svc.add_lead(1, ExtractionResult(company_name="ali_motors"))
    await svc.add_lead(1, ExtractionResult(company_name="alimotors"))

    found = await svc.search_leads(1, "ali_motors")

    assert [lead.id for lead in found] == [hit.id]


@pytest.mark.asyncio
async def test_bare_wildcards_find_nothing(session_factory):
    svc = LeadService(session_factory)
    await svc.add_lead(1, ExtractionResult(company_name="Обычная компания"))
    await svc.add_lead(1, ExtractionResult(company_name="Ali", phone_e164="+77001234567"))

    assert await svc.search_leads(1, "%") == []
    assert await svc.search_leads(1, "_") == []
    assert await svc.search_leads(1, "%%") == []


@pytest.mark.asyncio
async def test_search_with_a_backslash_does_not_crash(session_factory):
    svc = LeadService(session_factory)
    await svc.add_lead(1, ExtractionResult(company_name="C:\\temp"))

    assert [lead.company_name for lead in await svc.search_leads(1, "C:\\temp")] == ["C:\\temp"]


@pytest.mark.asyncio
async def test_ordinary_search_still_works(session_factory):
    svc = LeadService(session_factory)
    hit = await svc.add_lead(1, ExtractionResult(company_name="Ali Motors", city="Алматы"))
    await svc.add_lead(1, ExtractionResult(company_name="Ромашка"))

    assert [lead.id for lead in await svc.search_leads(1, "ali")] == [hit.id]
    assert [lead.id for lead in await svc.search_leads(1, "+7700")] == []
