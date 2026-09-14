"""Deduplication service: normalization + two match levels (strong auto-merge / medium ask)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.logging_config import log_json
from app.models import Lead
from app.schemas.extraction import ExtractionResult
from app.services.normalize import (
    normalize_city,
    normalize_company_name,
    normalize_phone,
    normalize_social_handle,
    normalize_website,
    phone_digits,
    phone_last_digits,
    website_key,
)

logger = logging.getLogger(__name__)

MatchLevel = Literal["strong", "medium", "none"]


@dataclass
class LeadFingerprint:
    """Comparable representation of a lead's identity fields."""

    phone_e164: str | None = None
    phone_digits: str | None = None
    website: str | None = None
    instagram: str | None = None
    telegram: str | None = None
    name_key: str | None = None
    city: str | None = None


@dataclass
class MatchResult:
    level: MatchLevel
    reason: str
    score: float | None = None
    lead_id: int | None = None


def _phones_match_strong(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    for n in (10, 9):
        if len(a) >= n and len(b) >= n and a[-n:] == b[-n:]:
            return True
    return False


def _phones_match_medium(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return len(a) >= 7 and len(b) >= 7 and a[-7:] == b[-7:]


def fingerprint_from_extraction(data: ExtractionResult) -> LeadFingerprint:
    """Build a fingerprint from freshly extracted data (normalized)."""
    phone_e164 = data.phone_e164 or normalize_phone(data.phone_raw)
    return LeadFingerprint(
        phone_e164=phone_e164,
        phone_digits=phone_digits(data.phone_raw or data.phone_e164),
        website=website_key(normalize_website(data.website)),
        instagram=normalize_social_handle(data.instagram),
        telegram=normalize_social_handle(data.telegram),
        name_key=normalize_company_name(data.company_name) or None,
        city=normalize_city(data.city),
    )


def fingerprint_from_lead(lead: Lead) -> LeadFingerprint:
    return LeadFingerprint(
        phone_e164=lead.phone,
        phone_digits=phone_digits(lead.phone),
        website=website_key(lead.website),
        instagram=normalize_social_handle(lead.instagram),
        telegram=normalize_social_handle(lead.telegram),
        name_key=normalize_company_name(lead.company_name) or None,
        city=normalize_city(lead.city),
    )


def classify_match(existing: LeadFingerprint, new: LeadFingerprint) -> MatchResult:
    """Pure function: classify a pair of fingerprints into strong/medium/none."""
    # Strong: exact phone / website / instagram / telegram.
    if _phones_match_strong(existing.phone_digits, new.phone_digits):
        return MatchResult("strong", "совпадение телефона")
    if existing.website and new.website and existing.website == new.website:
        return MatchResult("strong", "совпадение сайта")
    if existing.instagram and new.instagram and existing.instagram == new.instagram:
        return MatchResult("strong", "совпадение Instagram")
    if existing.telegram and new.telegram and existing.telegram == new.telegram:
        return MatchResult("strong", "совпадение Telegram")

    # Medium: fuzzy name >= 85 with matching city.
    if existing.name_key and new.name_key:
        ratio = fuzz.token_sort_ratio(existing.name_key, new.name_key)
        if ratio >= 85 and existing.city and new.city and existing.city == new.city:
            return MatchResult("medium", "похожее название в одном городе", score=ratio)

    # Medium: last 7 phone digits match but full number differs.
    if _phones_match_medium(existing.phone_digits, new.phone_digits) and not _phones_match_strong(
        existing.phone_digits, new.phone_digits
    ):
        return MatchResult("medium", "совпадение последних 7 цифр телефона")

    return MatchResult("none", "нет совпадения")


class DedupService:
    def __init__(self, session_factory: async_sessionmaker):
        self.session_factory = session_factory

    async def find_duplicate(
        self,
        fingerprint: LeadFingerprint,
        owner_user_id: int,
        exclude_lead_id: int | None = None,
    ) -> MatchResult | None:
        """Return the best strong/medium match among existing leads, or None."""
        async with self.session_factory() as session:
            stmt = select(Lead).where(
                Lead.owner_user_id == owner_user_id,
                Lead.deleted_at.is_(None),
            )
            result = await session.execute(stmt)
            leads = result.scalars().all()

        best: MatchResult | None = None
        for lead in leads:
            if exclude_lead_id is not None and lead.id == exclude_lead_id:
                continue
            match = classify_match(fingerprint_from_lead(lead), fingerprint)
            if match.level == "none":
                continue
            match.lead_id = lead.id
            if best is None or _level_rank(match.level) > _level_rank(best.level):
                best = match

        if best is not None:
            log_json(
                logger, 20, "dedup decision",
                action="duplicate_found", score=best.score, reason=best.reason,
            )
        return best


def _level_rank(level: MatchLevel) -> int:
    return {"none": 0, "medium": 1, "strong": 2}[level]
