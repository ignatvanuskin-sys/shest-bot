"""Lead service tests: create, merge (section 8), and undo."""
from __future__ import annotations

import pytest

from app.schemas.extraction import ExtractionResult
from app.services.lead_service import LeadService


@pytest.mark.asyncio
async def test_add_lead_creates(session_factory):
    svc = LeadService(session_factory)
    lead = await svc.add_lead(1, ExtractionResult(company_name="Ali", phone_e164="+77001234567"))
    assert lead.id is not None
    assert lead.company_name == "Ali"
    assert lead.phone == "+77001234567"


@pytest.mark.asyncio
async def test_merge_fills_empty_and_conflicts_to_comment(session_factory):
    svc = LeadService(session_factory)
    existing = await svc.add_lead(
        1, ExtractionResult(company_name="Ali", phone_e164="+77001234567", instagram="oldhandle")
    )
    incoming = ExtractionResult(
        company_name="Ali",
        phone_e164="+77001234567",
        website="https://ali.kz",
        instagram="newhandle",
    )
    merged = await svc.add_lead(1, incoming, merge_target_id=existing.id)

    db = await svc.get_lead(existing.id)
    assert db.website == "https://ali.kz"  # empty field filled
    assert db.instagram == "oldhandle"  # conflict: existing kept
    assert "альтернативный контакт instagram: newhandle" in (db.comment or "")

    assert merged.duplicate_of_id == existing.id
    assert merged.deleted_at is not None


@pytest.mark.asyncio
async def test_merge_prefer_new_contact(session_factory):
    svc = LeadService(session_factory)
    existing = await svc.add_lead(
        1, ExtractionResult(company_name="Ali", phone_e164="+77001234567", instagram="oldhandle")
    )
    incoming = ExtractionResult(company_name="Ali", phone_e164="+77001234567", instagram="newhandle")
    await svc.add_lead(1, incoming, merge_target_id=existing.id, prefer_new_contact=True)

    db = await svc.get_lead(existing.id)
    assert db.instagram == "newhandle"  # new contact becomes primary
    assert "альтернативный контакт instagram: oldhandle" in (db.comment or "")


@pytest.mark.asyncio
async def test_undo_created_soft_deletes(session_factory):
    svc = LeadService(session_factory)
    lead = await svc.add_lead(1, ExtractionResult(company_name="Ali"))
    result = await svc.undo_last(1)
    assert result is not None
    db = await svc.get_lead(lead.id)
    assert db.deleted_at is not None


@pytest.mark.asyncio
async def test_undo_merge_restores(session_factory):
    svc = LeadService(session_factory)
    existing = await svc.add_lead(
        1, ExtractionResult(company_name="Ali", phone_e164="+77001234567")
    )
    incoming = ExtractionResult(company_name="Ali", phone_e164="+77001234567", website="https://ali.kz")
    merged = await svc.add_lead(1, incoming, merge_target_id=existing.id)

    result = await svc.undo_last(1)
    assert result is not None

    db = await svc.get_lead(existing.id)
    assert db.website is None  # restored from snapshot

    restored = await svc.get_lead(merged.id)
    assert restored.deleted_at is None
    assert restored.duplicate_of_id is None
