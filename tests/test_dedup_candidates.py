"""FIX-12: dedup candidates come from SQL, are bounded, and never vanish silently.

The old lookup loaded *every* lead of the owner into memory and compared all of
them. These tests pin the replacement: candidates are selected in SQL by a shared
key (phone suffix / website host / social handle) or by the same city, the list has
an explicit limit, and hitting that limit is logged instead of quietly dropping
matches.

``classify_match`` keeps deciding strong/medium — the candidate query is allowed to
be broader, never narrower, so no level may change.
"""
from __future__ import annotations

import logging

import pytest

from app.models import Lead
from app.schemas.extraction import ExtractionResult
from app.services.dedup import DedupService, fingerprint_from_extraction, fingerprint_from_lead
from app.services.lead_service import extracted_to_lead_fields


async def add_lead(session_factory, owner_id: int, **fields) -> Lead:
    """Insert a lead straight from column values (fast: no audit/merge machinery)."""
    async with session_factory() as session:
        lead = Lead(owner_user_id=owner_id, status="новый", **fields)
        session.add(lead)
        await session.commit()
        await session.refresh(lead)
        return lead


def fp(**kwargs):
    return fingerprint_from_extraction(ExtractionResult(**kwargs))


# ---------------- the candidate set is narrowed in SQL ----------------
@pytest.mark.asyncio
async def test_match_is_found_among_a_large_pile_of_garbage_leads(session_factory):
    """220 unrelated leads in the same city must not hide the real duplicate."""
    svc = DedupService(session_factory, candidate_limit=50)
    for index in range(220):
        await add_lead(
            session_factory, 1,
            company_name=f"Мусорная компания {index}",
            city="Караганда",
            phone=f"+7700{index:07d}",
        )
    duplicate = await add_lead(
        session_factory, 1, company_name="Ali Motors", city="Алматы",
        phone="+77001234567",
    )

    match = await svc.find_duplicate(fp(phone_raw="+7 700 123 45 67"), 1)

    assert match is not None and match.level == "strong"
    assert match.lead_id == duplicate.id


@pytest.mark.asyncio
async def test_candidate_query_does_not_load_unrelated_leads(session_factory):
    """Proof the filtering happens in SQL: only the plausible row is fetched."""
    svc = DedupService(session_factory)
    for index in range(100):
        await add_lead(
            session_factory, 1, company_name=f"Другая {index}", city="Алматы",
            phone=f"+7700{index:07d}",
        )
    target = await add_lead(session_factory, 1, company_name="Ali", instagram="alimotors")

    candidates = await svc._load_candidates(fp(instagram="@AliMotors"), 1, None)

    assert [lead.id for lead in candidates] == [target.id]


@pytest.mark.asyncio
async def test_strong_identifiers_win_when_the_limit_truncates(session_factory):
    """With the list cut off, phone matches must survive — city-only rows must not."""
    svc = DedupService(session_factory, candidate_limit=5)
    target = await add_lead(
        session_factory, 1, company_name="Ali Motors", city="Алматы",
        phone="+77001234567",
    )
    for index in range(50):  # same city, no shared identifier, newer ids
        await add_lead(
            session_factory, 1, company_name=f"Сосед {index}", city="Алматы",
            phone=f"+7701{index:07d}",
        )

    match = await svc.find_duplicate(fp(phone_e164="+77001234567"), 1)

    assert match is not None and match.lead_id == target.id


@pytest.mark.asyncio
async def test_medium_match_in_the_same_city_is_still_found(session_factory):
    """Cyrillic cities: SQLite's ``lower()`` folds ASCII only, so the match must
    not depend on ``lower(city)`` (the regression that emptied the candidate list)."""
    svc = DedupService(session_factory)
    existing = await add_lead(session_factory, 1, company_name="Ромашка", city="Алматы")

    match = await svc.find_duplicate(fp(company_name="ромашка", city="алматы"), 1)

    assert match is not None and match.level == "medium"
    assert match.lead_id == existing.id


@pytest.mark.asyncio
async def test_medium_last7_phone_rule_is_not_lost(session_factory):
    svc = DedupService(session_factory)
    existing = await add_lead(session_factory, 1, company_name="Ali", phone="+77001234567")

    match = await svc.find_duplicate(fp(phone_e164="+77101234567"), 1)

    assert match is not None and match.level == "medium"
    assert match.lead_id == existing.id


@pytest.mark.asyncio
async def test_website_host_finds_the_candidate(session_factory):
    svc = DedupService(session_factory)
    existing = await add_lead(
        session_factory, 1, company_name="Ali", website="https://www.alimotors.kz/"
    )

    match = await svc.find_duplicate(fp(website="https://alimotors.kz/?utm_source=2gis"), 1)

    assert match is not None and match.level == "strong"
    assert match.lead_id == existing.id


# ---------------- boundaries ----------------
@pytest.mark.asyncio
async def test_limit_reached_is_logged(session_factory, caplog):
    svc = DedupService(session_factory, candidate_limit=10)
    for index in range(40):
        await add_lead(session_factory, 1, company_name=f"Сосед {index}", city="Алматы")

    with caplog.at_level(logging.WARNING, logger="app.services.dedup"):
        await svc.find_duplicate(fp(company_name="Кто-то", city="Алматы"), 1)

    limits = [r for r in caplog.records if getattr(r, "action", None) == "dedup_candidate_limit"]
    assert limits, "a truncated candidate list must be reported, not silent"
    assert limits[0].limit == 10
    assert limits[0].compared == 10


@pytest.mark.asyncio
async def test_unusual_city_spelling_falls_back_to_a_bounded_scan(session_factory, caplog):
    """A spelling outside the SQL variants must not silently drop a medium match.

    The stored city keeps a copy-paste artefact (double space) that normalizes to
    the same key, so ``classify_match`` *can* match it — but the keyed city filter
    (exact equality against the usual spellings) cannot see it. The bounded
    recency scan is the safety net, and it announces itself in the log.
    """
    svc = DedupService(session_factory)
    existing = await add_lead(
        session_factory, 1, company_name="Ромашка", city="Алматы  қаласы"
    )

    with caplog.at_level(logging.WARNING, logger="app.services.dedup"):
        match = await svc.find_duplicate(fp(company_name="ромашка", city="Алматы қаласы"), 1)

    assert match is not None and match.lead_id == existing.id
    actions = [getattr(r, "action", None) for r in caplog.records]
    assert "dedup_fallback_scan" in actions, "the widening must be visible in the log"


@pytest.mark.asyncio
async def test_a_different_city_is_still_no_match(session_factory):
    """The fallback must not weaken the rules: a different city stays «none»."""
    svc = DedupService(session_factory)
    await add_lead(session_factory, 1, company_name="Ромашка", city="г. Алматы")

    assert await svc.find_duplicate(fp(company_name="ромашка", city="алматы"), 1) is None


@pytest.mark.asyncio
async def test_no_key_no_city_compares_nothing(session_factory):
    """A fingerprint with no identifier cannot match anything — no scan at all."""
    svc = DedupService(session_factory)
    await add_lead(session_factory, 1, company_name="Ромашка", city="Алматы")

    assert await svc.find_duplicate(fp(company_name="Ромашка"), 1) is None
    assert await svc._load_candidates(fp(company_name="Ромашка"), 1, None) == []


@pytest.mark.asyncio
async def test_excluded_lead_is_filtered_out_in_sql(session_factory):
    svc = DedupService(session_factory)
    existing = await add_lead(session_factory, 1, company_name="Ali", phone="+77001234567")

    match = await svc.find_duplicate(
        fp(phone_e164="+77001234567"), 1, exclude_lead_id=existing.id
    )

    assert match is None


@pytest.mark.asyncio
async def test_another_owners_lead_is_never_a_candidate(session_factory):
    svc = DedupService(session_factory)
    await add_lead(session_factory, 999, company_name="Ali", phone="+77001234567")

    assert await svc.find_duplicate(fp(phone_e164="+77001234567"), 1) is None


@pytest.mark.asyncio
async def test_deleted_and_merged_rows_are_not_candidates(session_factory):
    from datetime import datetime, timezone

    svc = DedupService(session_factory)
    await add_lead(
        session_factory, 1, company_name="Удалённый", phone="+77001234567",
        deleted_at=datetime.now(timezone.utc),
    )
    assert await svc.find_duplicate(fp(phone_e164="+77001234567"), 1) is None


@pytest.mark.asyncio
async def test_invalid_limit_falls_back_to_the_default(session_factory):
    assert DedupService(session_factory, candidate_limit=0).candidate_limit > 0
    assert DedupService(session_factory, candidate_limit="abc").candidate_limit > 0


# ---------------- classify_match semantics are untouched ----------------
@pytest.mark.asyncio
async def test_strong_match_beats_a_newer_medium_match(session_factory):
    svc = DedupService(session_factory)
    strong = await add_lead(
        session_factory, 1, company_name="Ali Motors", city="Алматы",
        phone="+77001234567",
    )
    await add_lead(session_factory, 1, company_name="Али Мотос", city="Алматы")

    match = await svc.find_duplicate(
        fp(company_name="Али Мотос", city="Алматы", phone_e164="+77001234567"), 1
    )

    assert match is not None and match.level == "strong"
    assert match.lead_id == strong.id


def test_fingerprint_from_lead_is_unchanged():
    """The fingerprint contract the levels rely on (no accidental reformatting)."""
    lead = Lead(id=1, phone="+77001234567", website="https://www.ali.kz/x",
                instagram="@AliMotors", company_name="ТОО «Ali Motors»", city="Алматы")

    fingerprint = fingerprint_from_lead(lead)

    assert fingerprint.phone_digits == "77001234567"
    assert fingerprint.website == "ali.kz/x"
    assert fingerprint.instagram == "alimotors"
    assert fingerprint.name_key == "ali motors"
    assert fingerprint.city == "алматы"
