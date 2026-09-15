"""SQLAlchemy ORM models — the local source of truth for all leads."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False
    )

    company_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(Text, nullable=True)
    city: Mapped[str | None] = mapped_column(Text, nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone: Mapped[str | None] = mapped_column(Text, nullable=True)  # normalized E.164
    # The *original* spelling of the phone as it came from the source (ТЗ §7) —
    # kept so the normalization can be audited after the fact.
    phone_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    whatsapp_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    instagram: Mapped[str | None] = mapped_column(Text, nullable=True)  # without @
    telegram: Mapped[str | None] = mapped_column(Text, nullable=True)  # without @
    website: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    services: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON array
    tags: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON array
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    reviews_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    contact_person: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(Text, nullable=False, default="новый")
    priority: Mapped[str | None] = mapped_column(Text, nullable=True)
    pain_point: Mapped[str | None] = mapped_column(Text, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_contact_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sheet_row: Mapped[int | None] = mapped_column(Integer, nullable=True)  # cached sheet row
    duplicate_of_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("leads.id"), nullable=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def mergeable_fields(self) -> dict[str, object]:
        """Fields that participate in merge; used for audit snapshots."""
        names = (
            "company_name",
            "category",
            "city",
            "address",
            "phone",
            "phone_raw",
            "whatsapp_number",
            "email",
            "instagram",
            "telegram",
            "website",
            "source",
            "source_url",
            "services",
            "tags",
            "description",
            "rating",
            "reviews_count",
            "contact_person",
            "status",
            "comment",
        )
        return {name: getattr(self, name) for name in names}


class LeadSession(Base):
    __tablename__ = "lead_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="collecting")
    combined_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resulting_lead_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("leads.id"), nullable=True
    )


class RawMessage(Base):
    __tablename__ = "raw_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("lead_sessions.id"), nullable=True, index=True
    )
    lead_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("leads.id"), nullable=True, index=True
    )
    message_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)


class ExtractionLog(Base):
    __tablename__ = "extraction_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("lead_sessions.id"), nullable=True, index=True
    )
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd_est: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ТЗ §11: the model's answer itself, truncated (see MAX_RESPONSE_BODY_CHARS).
    # DEBUG-level data: written only when LOG_LLM_BODIES is on, and purged after
    # EXTRACTION_LOG_RETENTION_DAYS. Never written to the stdout logs.
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)


class User(Base):
    __tablename__ = "users"

    telegram_user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[str] = mapped_column(Text, nullable=False, default="owner")
    added_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    action: Mapped[str] = mapped_column(Text, nullable=False)  # created|updated|merged|deleted|restored
    lead_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
