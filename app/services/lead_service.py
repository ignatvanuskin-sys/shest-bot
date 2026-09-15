"""Lead service — business logic over the local SQLite source of truth."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.logging_config import log_json
from app.models import AuditLog, ExtractionLog, Lead, LeadSession, RawMessage
from app.schemas.extraction import ExtractionResult
from app.services.extraction import FREE_MODEL_SUFFIXES
from app.services.normalize import (
    LIKE_ESCAPE,
    escape_like,
    normalize_phone,
    normalize_social_handle,
    normalize_website,
)

logger = logging.getLogger(__name__)

# Contact fields where a merge conflict (both non-empty, different) is possible.
CONTACT_FIELDS = ("phone", "whatsapp_number", "email", "instagram", "telegram", "website")
# Fields simply filled in when empty.
FILL_FIELDS = (
    "company_name", "category", "city", "address", "description",
    "rating", "reviews_count", "contact_person", "source", "source_url",
    "phone_raw",
)

# Audit actions /undo can reverse, and the marker it writes when it did.
REVERSIBLE_ACTIONS = ("created", "merged")
REVERSAL_MARKERS = {"created": "deleted", "merged": "restored"}
# How far back the undo chain looks for an action that is not reversed yet.
UNDO_SCAN_LIMIT = 100

# Session statuses that mean "a dialog was still in flight". The FSM and the message
# buffer live in memory (MemoryStorage), so after a restart such a session can never
# be finished — it must not stay in the database as if the user were still typing.
UNFINISHED_SESSION_STATUSES = ("collecting", "review", "editing")

# Upper bound for one automatic resync pass (keeps a huge backlog from hammering
# the Sheets API in a single burst).
RESYNC_BATCH_LIMIT = 200


@dataclass(frozen=True)
class SheetUndo:
    """What an undo has to mirror into the sheet (done outside the DB transaction)."""

    kind: Literal["clear", "restore"]
    row: int | None = None
    lead_id: int | None = None


def extracted_to_lead_fields(owner_id: int, data: ExtractionResult) -> dict:
    """Map a validated ExtractionResult onto Lead column values."""
    return {
        "owner_user_id": owner_id,
        "company_name": data.company_name,
        "category": data.category,
        "city": data.city,
        "address": data.address,
        "phone": data.phone_e164 or normalize_phone(data.phone_raw),
        # ТЗ §7: keep the raw spelling next to the normalized one, so the audit can
        # see what the source actually said (FIX-11).
        "phone_raw": data.phone_raw,
        "whatsapp_number": normalize_phone(data.whatsapp_number),
        "email": data.email,
        "instagram": normalize_social_handle(data.instagram),
        "telegram": normalize_social_handle(data.telegram),
        "website": normalize_website(data.website),
        "source": data.source_guess,
        # FIX-13: same utm/fbclid cleaning as ``website`` — a source link copied from
        # 2GIS/Instagram otherwise keeps its tracking noise for ever.
        "source_url": normalize_website(data.source_url),
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


def describe_cost(cost_usd: float, extractions: int = 0, free_extractions: int = 0) -> str:
    """Human wording for the «расход на AI» line of /stats (FIX-11).

    A zero is only reported as a *free model* when every logged extraction really
    ran on a ``:free`` model; when the provider reported no price for a paid model
    the line says so instead of implying a measured 0.
    """
    if cost_usd > 0:
        return f"${cost_usd:.6f}"
    if extractions <= 0:
        return "нет данных (обращений к AI ещё не было)"
    if free_extractions >= extractions:
        return "$0 (бесплатная модель)"
    return "$0 (тариф модели не учтён)"


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

    async def cancel_unfinished_sessions(
        self, statuses: tuple[str, ...] = UNFINISHED_SESSION_STATUSES
    ) -> list[int]:
        """Mark sessions interrupted by a restart as ``cancelled``; returns their ids.

        Their FSM state and buffered text died with the process, so ``collecting`` /
        ``review`` rows are dead ends. Leaving them as-is made the database claim the
        user was still mid-dialog.
        """
        async with self.session_factory() as session:
            result = await session.execute(
                select(LeadSession).where(LeadSession.status.in_(statuses))
            )
            hung = list(result.scalars().all())
            for lead_session in hung:
                lead_session.status = "cancelled"
            await session.commit()
            return sorted(lead_session.id for lead_session in hung)

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

    async def get_unsynced_leads_all_owners(
        self, limit: int = RESYNC_BATCH_LIMIT
    ) -> list[Lead]:
        """The resync queue (leads with no sheet row yet) across every owner.

        Same shape as :meth:`get_unsynced_leads`, but not scoped to one user: the
        periodic worker has no owner context. Merged/deleted rows are excluded here
        and refused again by ``sheets.is_syncable`` — they are audit history, not
        leads waiting for a row.
        """
        async with self.session_factory() as session:
            result = await session.execute(
                select(Lead)
                .where(
                    Lead.deleted_at.is_(None),
                    Lead.duplicate_of_id.is_(None),
                    Lead.sheet_row.is_(None),
                )
                .order_by(Lead.id)
                .limit(limit)
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
        # FIX-17: ``%``/``_`` in the query are wildcards for LIKE, so a search for
        # «100%» used to return the whole base. Escape them and match literally.
        pattern = f"%{escape_like(query.strip())}%"
        async with self.session_factory() as session:
            result = await session.execute(
                select(Lead)
                .where(
                    Lead.owner_user_id == owner_id,
                    Lead.deleted_at.is_(None),
                    (Lead.company_name.ilike(pattern, escape=LIKE_ESCAPE))
                    | (Lead.phone.ilike(pattern, escape=LIKE_ESCAPE))
                    | (Lead.instagram.ilike(pattern, escape=LIKE_ESCAPE))
                    | (Lead.telegram.ilike(pattern, escape=LIKE_ESCAPE))
                    | (Lead.website.ilike(pattern, escape=LIKE_ESCAPE))
                    | (Lead.email.ilike(pattern, escape=LIKE_ESCAPE)),
                )
                .order_by(Lead.id.desc())
                .limit(10)
            )
            return list(result.scalars().all())

    async def get_stats(self, owner_id: int) -> dict:
        """Counters for /stats, scoped to one owner (ТЗ §7/§11).

        The AI counters come from ``extraction_logs`` attributed through the owner's
        lead sessions — the same rule the cost sum always used. ``free_extractions``
        lets the caller say «0 (бесплатная модель)» instead of pretending a zero cost
        is a measured spend.
        """
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
            usage = (
                await session.execute(
                    select(
                        func.count(ExtractionLog.id),
                        func.coalesce(
                            func.sum(
                                case((ExtractionLog.success.is_(True), 1), else_=0)
                            ),
                            0,
                        ),
                        func.coalesce(func.sum(ExtractionLog.tokens_in), 0),
                        func.coalesce(func.sum(ExtractionLog.tokens_out), 0),
                        func.coalesce(func.sum(ExtractionLog.cost_usd_est), 0.0),
                        func.coalesce(
                            func.sum(
                                case(
                                    (
                                        or_(
                                            *[
                                                ExtractionLog.model.like(f"%{suffix}")
                                                for suffix in FREE_MODEL_SUFFIXES
                                            ]
                                        ),
                                        1,
                                    ),
                                    else_=0,
                                )
                            ),
                            0,
                        ),
                    )
                    .join(LeadSession, ExtractionLog.session_id == LeadSession.id)
                    .where(LeadSession.telegram_user_id == owner_id)
                )
            ).one()
        extractions, extractions_ok, tokens_in, tokens_out, cost, free_extractions = usage
        return {
            "total": int(total or 0),
            "week": int(week or 0),
            "duplicates": int(duplicates or 0),
            "extractions": int(extractions or 0),
            "extractions_ok": int(extractions_ok or 0),
            "tokens_in": int(tokens_in or 0),
            "tokens_out": int(tokens_out or 0),
            "cost_usd": float(cost or 0.0),
            "free_extractions": int(free_extractions or 0),
        }

    # ---------- undo ----------
    async def undo_last(self, owner_id: int, sheets=None) -> str | None:
        """Reverse the actor's latest not-yet-reversed created/merged action.

        Walks the audit log newest-first and skips entries that already carry a
        reversal marker, so several undos in a row work (the old query always
        returned the same oldest row and answered «нечего откатывать»).

        When *sheets* is given the undo is mirrored into the table as well:
        a reversed creation blanks the lead's row, a reversed merge rewrites it
        from the audit snapshot. Returns a human description, or None if nothing
        is left to undo.
        """
        message: str | None = None
        sheet_action: SheetUndo | None = None
        async with self.session_factory() as session:
            result = await session.execute(
                select(AuditLog)
                .where(AuditLog.actor_id == owner_id, AuditLog.action.in_(REVERSIBLE_ACTIONS))
                .order_by(AuditLog.id.desc())
                .limit(UNDO_SCAN_LIMIT)
            )
            for entry in result.scalars().all():
                if await self._already_reversed(session, entry):
                    continue
                if entry.action == "created":
                    outcome = await self._undo_created(session, owner_id, entry)
                else:
                    outcome = await self._undo_merged(session, owner_id, entry)
                if outcome is None:
                    # Nothing reversible here (row gone / already deleted): the next
                    # older action still can be.
                    continue
                message, sheet_action = outcome
                await session.commit()
                break

        if message is None:
            return None
        # The sheet is written after the DB session is closed: the network call must
        # not hold a SQLite connection (and its failure must not undo the undo).
        note = await self._reflect_undo_in_sheet(sheets, sheet_action)
        return f"{message}.{note}" if note else message

    async def _already_reversed(self, session, entry: AuditLog) -> bool:
        """Whether a newer reversal marker for *entry* exists in the audit log."""
        marker = REVERSAL_MARKERS[entry.action]
        stmt = (
            select(AuditLog.id)
            .where(
                AuditLog.lead_id == entry.lead_id,
                AuditLog.action == marker,
                AuditLog.id > entry.id,
            )
            .limit(1)
        )
        return await session.scalar(stmt) is not None

    async def _undo_created(self, session, owner_id: int, entry: AuditLog):
        lead = await session.get(Lead, entry.lead_id) if entry.lead_id else None
        if lead is None or lead.deleted_at is not None:
            return None
        lead.deleted_at = datetime.now(timezone.utc)
        session.add(
            AuditLog(
                actor_id=owner_id,
                action="deleted",
                lead_id=lead.id,
                details=json.dumps({"lead_id": lead.id}),
            )
        )
        log_json(logger, 20, "lead creation undone", lead_id=lead.id, action="undo_created")
        return (
            f"Отменено создание лида #{lead.id}",
            SheetUndo("clear", row=lead.sheet_row, lead_id=lead.id),
        )

    async def _undo_merged(self, session, owner_id: int, entry: AuditLog):
        try:
            details = json.loads(entry.details or "{}")
        except ValueError:
            return None
        target = await session.get(Lead, entry.lead_id) if entry.lead_id else None
        if target is None:
            return None
        duplicate_id = details.get("duplicate_lead_id")
        snapshot = details.get("snapshot") or {}
        for key, value in snapshot.items():
            if hasattr(target, key):
                setattr(target, key, value)
        target.last_action = "restored"
        duplicate = await session.get(Lead, duplicate_id) if duplicate_id else None
        if duplicate is not None:
            duplicate.duplicate_of_id = None
            duplicate.deleted_at = None
            duplicate.last_action = "restored"
        session.add(
            AuditLog(
                actor_id=owner_id,
                action="restored",
                lead_id=target.id,
                details=json.dumps({"from": "merged", "lead_id": target.id}),
            )
        )
        log_json(logger, 20, "lead merge undone", lead_id=target.id, action="undo_merged")
        return (
            f"Отменено объединение лида #{target.id}",
            SheetUndo("restore", row=target.sheet_row, lead_id=target.id),
        )

    async def _reflect_undo_in_sheet(self, sheets, sheet_action: SheetUndo | None) -> str | None:
        """Mirror an undo into the sheet. Returns a user-facing note on failure.

        The database is already the source of truth at this point, so a failing
        sheet write must never roll the undo back — it is reported instead.
        """
        if sheets is None or sheet_action is None:
            return None
        if not getattr(sheets, "configured", False):
            log_json(
                logger, 30, "undo not mirrored into sheets (not configured)",
                action="undo_sheet_skipped", lead_id=sheet_action.lead_id,
            )
            return None
        try:
            if sheet_action.kind == "clear":
                if not sheet_action.row:
                    return None
                if await sheets.clear_row(sheet_action.row, lead_id=sheet_action.lead_id):
                    return None
                return " Строку в таблице очистить не удалось — повторите /undo."
            lead = await self.get_lead(sheet_action.lead_id)
            if lead is None:
                return None
            before = lead.sheet_row
            row = await sheets.sync_lead(lead)
            if row is None:
                return " Таблицу обновить не удалось — повторите /undo."
            if row != before:
                await self.update_lead(lead.id, sheet_row=row)
            return None
        except Exception:
            logger.exception("failed to mirror undo into sheets")
            return " Таблицу обновить не удалось (подробности в логах)."
