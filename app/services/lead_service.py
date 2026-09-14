"""Lead service — business logic over the local SQLite source of truth."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.logging_config import log_json
from app.models import AuditLog, ExtractionLog, Lead, LeadSession, RawMessage
from app.schemas.extraction import ExtractionResult
from app.services.normalize import normalize_phone, normalize_social_handle, normalize_website

logger = logging.getLogger(__name__)

# Contact fields where a merge conflict (both non-empty, different) is possible.
CONTACT_FIELDS = ("phone", "whatsapp_number", "email", "instagram", "telegram", "website")
# Fields simply filled in when empty.
FILL_FIELDS = (
    "company_name", "category", "city", "address", "description",
    "rating", "reviews_count", "contact_person", "source", "source_url",
)


def extracted_to_lead_fields(owner_id: int, data: ExtractionResult) -> dict:
    """Map a validated ExtractionResult onto Lead column values."""
    return {
        "owner_user_id": owner_id,
        "company_name": data.company_name,
        "category": data.category,
        "city": data.city,
        "address": data.address,
        "phone": data.phone_e164 or normalize_phone(data.phone_raw),
        "whatsapp_number": normalize_phone(data.whatsapp_number),
        "email": data.email,
        "instagram": normalize_social_handle(data.instagram),
        "telegram": normalize_social_handle(data.telegram),
        "website": normalize_website(data.website),
        "source": data.source_guess,
        "source_url": data.source_url,
        "services": json.dumps(data.services, ensure_ascii=False) if data.services else None,
        "tags": json.dumps(data.tags, ensure_ascii=False) if data.tags else None,
        "description": data.description,
        "rating": data.rating,
        "reviews_count": data.reviews_count,
        "contact_person": data.contact_person,
        "needs_review": bool(data.uncertain_fields),
        "status": "новый",
        "last_action": "created",
    }


def _merge_json_arrays(existing: str | None, incoming: str | None) -> str | None:
    def _load(v: str | None) -> list:
        if not v:
            return []
        try:
            parsed = json.loads(v)
        except (TypeError, ValueError):
            return []
        return parsed if isinstance(parsed, list) else []

    combined = list(dict.fromkeys(_load(existing) + _load(incoming)))
    return json.dumps(combined, ensure_ascii=False) if combined else None


class LeadService:
    def __init__(self, session_factory: async_sessionmaker):
        self.session_factory = session_factory

    # ---------- sessions & raw messages ----------
    async def create_session(self, telegram_user_id: int) -> int:
        async with self.session_factory() as session:
            lead_session = LeadSession(telegram_user_id=telegram_user_id, status="collecting")
            session.add(lead_session)
            await session.commit()
            await session.refresh(lead_session)
            return lead_session.id

    async def update_session(self, session_id: int, **fields) -> None:
        async with self.session_factory() as session:
            lead_session = await session.get(LeadSession, session_id)
            if lead_session is None:
                return
            for key, value in fields.items():
                setattr(lead_session, key, value)
            await session.commit()

    async def add_raw_message(
        self, session_id: int | None, lead_id: int | None, text: str
    ) -> None:
        async with self.session_factory() as session:
            session.add(RawMessage(session_id=session_id, lead_id=lead_id, message_text=text))
            await session.commit()

    async def link_raw_messages_to_lead(self, session_id: int, lead_id: int) -> None:
        async with self.session_factory() as session:
            result = await session.execute(
                select(RawMessage).where(RawMessage.session_id == session_id)
            )
            for raw in result.scalars().all():
                raw.lead_id = lead_id
            await session.commit()

    # ---------- leads ----------
    async def get_lead(self, lead_id: int) -> Lead | None:
        async with self.session_factory() as session:
            return await session.get(Lead, lead_id)

    async def add_lead(
        self,
        owner_id: int,
        data: ExtractionResult,
        session_id: int | None = None,
        merge_target_id: int | None = None,
        prefer_new_contact: bool = False,
    ) -> Lead:
        """Create a lead (optionally merged into an existing one). Returns the *new* lead row."""
        async with self.session_factory() as session:
            lead = Lead(**extracted_to_lead_fields(owner_id, data))
            session.add(lead)
            await session.flush()  # assign lead.id

            merged = False
            if merge_target_id is not None:
                existing = await session.get(Lead, merge_target_id)
                if existing is not None:
                    snapshot = existing.mergeable_fields()
                    self._merge_into(existing, lead, prefer_new_contact)
                    lead.duplicate_of_id = existing.id
                    lead.deleted_at = datetime.now(timezone.utc)
                    lead.last_action = "merged"
                    existing.last_action = "merged"
                    session.add(
                        AuditLog(
                            actor_id=owner_id,
                            action="merged",
                            lead_id=existing.id,
                            details=json.dumps(
                                {"duplicate_lead_id": lead.id, "snapshot": snapshot},
                                ensure_ascii=False,
                                default=str,
                            ),
                        )
                    )
                    merged = True
                    log_json(logger, 20, "lead merged", lead_id=existing.id, action="merged")

            if not merged:
                session.add(
                    AuditLog(
                        actor_id=owner_id,
                        action="created",
                        lead_id=lead.id,
                        details=json.dumps({"lead_id": lead.id}),
                    )
                )
                log_json(logger, 20, "lead created", lead_id=lead.id, action="created")

            if session_id is not None:
                lead_session = await session.get(LeadSession, session_id)
                if lead_session is not None:
                    lead_session.resulting_lead_id = lead.id
                    lead_session.status = "done"
                await self._link_raw_messages(session, session_id, lead.id)

            await session.commit()
            await session.refresh(lead)
            return lead

    async def _link_raw_messages(self, session, session_id: int, lead_id: int) -> None:
        result = await session.execute(
            select(RawMessage).where(RawMessage.session_id == session_id)
        )
        for raw in result.scalars().all():
            raw.lead_id = lead_id

    def _merge_into(self, existing: Lead, incoming: Lead, prefer_new_contact: bool) -> None:
        """Mutate ``existing`` with incoming data following the merge rules (section 8)."""
        conflicts: list[str] = []
        for field in FILL_FIELDS:
            old = getattr(existing, field)
            new = getattr(incoming, field)
            if (old is None or old == "") and (new is not None and new != ""):
                setattr(existing, field, new)

        for field in CONTACT_FIELDS:
            old = getattr(existing, field)
            new = getattr(incoming, field)
            if (old is None or old == "") and (new is not None and new != ""):
                setattr(existing, field, new)
            elif (old is not None and old != "") and (new is not None and new != "") and old != new:
                if prefer_new_contact:
                    conflicts.append(f"альтернативный контакт {field}: {old}")
                    setattr(existing, field, new)
                else:
                    conflicts.append(f"альтернативный контакт {field}: {new}")

        # Union services/tags.
        if incoming.services or existing.services:
            existing.services = _merge_json_arrays(existing.services, incoming.services)
        if incoming.tags or existing.tags:
            existing.tags = _merge_json_arrays(existing.tags, incoming.tags)

        if incoming.needs_review:
            existing.needs_review = True

        if conflicts:
            note = "; ".join(conflicts)
            existing.comment = (existing.comment + " | " + note) if existing.comment else note

    async def update_lead(self, lead_id: int, **fields) -> Lead | None:
        async with self.session_factory() as session:
            lead = await session.get(Lead, lead_id)
            if lead is None:
                return None
            for key, value in fields.items():
                setattr(lead, key, value)
            await session.commit()
            return lead

    async def get_unsynced_leads(self, owner_id: int) -> list[Lead]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(Lead).where(
                    Lead.owner_user_id == owner_id,
                    Lead.deleted_at.is_(None),
                    Lead.duplicate_of_id.is_(None),
                    Lead.sheet_row.is_(None),
                )
            )
            return list(result.scalars().all())

    # ---------- queries ----------
    async def get_last_leads(self, owner_id: int, limit: int = 5) -> list[Lead]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(Lead)
                .where(Lead.owner_user_id == owner_id, Lead.deleted_at.is_(None))
                .order_by(Lead.id.desc())
                .limit(limit)
            )
            return list(result.scalars().all())

    async def search_leads(self, owner_id: int, query: str) -> list[Lead]:
        pattern = f"%{query.strip()}%"
        async with self.session_factory() as session:
            result = await session.execute(
                select(Lead)
                .where(
                    Lead.owner_user_id == owner_id,
                    Lead.deleted_at.is_(None),
                    (Lead.company_name.ilike(pattern))
                    | (Lead.phone.ilike(pattern))
                    | (Lead.instagram.ilike(pattern))
                    | (Lead.telegram.ilike(pattern))
                    | (Lead.website.ilike(pattern))
                    | (Lead.email.ilike(pattern)),
                )
                .order_by(Lead.id.desc())
                .limit(10)
            )
            return list(result.scalars().all())

    async def get_stats(self, owner_id: int) -> dict:
        now = datetime.now(timezone.utc)
        week_ago = now - timedelta(days=7)
        async with self.session_factory() as session:
            total = await session.scalar(
                select(func.count()).select_from(Lead).where(
                    Lead.owner_user_id == owner_id,
                    Lead.deleted_at.is_(None),
                    Lead.duplicate_of_id.is_(None),
                )
            )
            week = await session.scalar(
                select(func.count()).select_from(Lead).where(
                    Lead.owner_user_id == owner_id,
                    Lead.deleted_at.is_(None),
                    Lead.duplicate_of_id.is_(None),
                    Lead.created_at >= week_ago,
                )
            )
            duplicates = await session.scalar(
                select(func.count()).select_from(Lead).where(
                    Lead.owner_user_id == owner_id, Lead.duplicate_of_id.is_not(None)
                )
            )
            cost = await session.scalar(
                select(func.coalesce(func.sum(ExtractionLog.cost_usd_est), 0.0))
                .join(LeadSession, ExtractionLog.session_id == LeadSession.id)
                .where(LeadSession.telegram_user_id == owner_id)
            )
        return {
            "total": int(total or 0),
            "week": int(week or 0),
            "duplicates": int(duplicates or 0),
            "cost_usd": float(cost or 0.0),
        }

    # ---------- undo ----------
    async def undo_last(self, owner_id: int) -> str | None:
        """Reverse the actor's latest created/merged action. Returns a human description."""
        async with self.session_factory() as session:
            result = await session.execute(
                select(AuditLog)
                .where(AuditLog.actor_id == owner_id, AuditLog.action.in_(("created", "merged")))
                .order_by(AuditLog.id.desc())
                .limit(1)
            )
            entry = result.scalars().first()
            if entry is None:
                return None

            if entry.action == "created":
                lead = await session.get(Lead, entry.lead_id)
                if lead is not None and lead.deleted_at is None:
                    lead.deleted_at = datetime.now(timezone.utc)
                    session.add(AuditLog(actor_id=owner_id, action="deleted", lead_id=lead.id,
                                         details=json.dumps({"lead_id": lead.id})))
                    await session.commit()
                    return f"Отменено создание лида #{lead.id}"
                return None

            if entry.action == "merged":
                try:
                    details = json.loads(entry.details or "{}")
                except ValueError:
                    return None
                target = await session.get(Lead, entry.lead_id)
                duplicate_id = details.get("duplicate_lead_id")
                snapshot = details.get("snapshot") or {}
                if target is not None:
                    for key, value in snapshot.items():
                        if hasattr(target, key):
                            setattr(target, key, value)
                    target.last_action = "restored"
                duplicate = await session.get(Lead, duplicate_id) if duplicate_id else None
                if duplicate is not None:
                    duplicate.duplicate_of_id = None
                    duplicate.deleted_at = None
                    duplicate.last_action = "restored"
                session.add(AuditLog(actor_id=owner_id, action="restored", lead_id=entry.lead_id,
                                     details=json.dumps({"from": "merged", "lead_id": entry.lead_id})))
                await session.commit()
                return f"Отменено объединение лида #{entry.lead_id}"
            return None
