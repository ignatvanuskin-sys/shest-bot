"""Deduplication service: normalization + two match levels (strong auto-merge / medium ask)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from rapidfuzz import fuzz
from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.logging_config import log_json
from app.models import Lead
from app.schemas.extraction import ExtractionResult
from app.services.normalize import (
    LIKE_ESCAPE,
    escape_like,
    normalize_city,
    normalize_company_name,
    normalize_phone,
    normalize_social_handle,
    normalize_website,
    phone_digits,
    website_key,
)

logger = logging.getLogger(__name__)

MatchLevel = Literal["strong", "medium", "none"]

# Upper bound for the candidate rows one dedup lookup may pull out of the database.
# The comparison itself is in-memory, so an unbounded ``SELECT *`` over the whole
# owner's base was the bottleneck (FIX-12). Overridable via DEDUP_CANDIDATE_LIMIT.
DEFAULT_CANDIDATE_LIMIT = 500


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
    def __init__(
        self,
        session_factory: async_sessionmaker,
        candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
    ):
        self.session_factory = session_factory
        try:
            limit = int(candidate_limit)
        except (TypeError, ValueError):
            limit = DEFAULT_CANDIDATE_LIMIT
        self.candidate_limit = limit if limit > 0 else DEFAULT_CANDIDATE_LIMIT

    async def find_duplicate(
        self,
        fingerprint: LeadFingerprint,
        owner_user_id: int,
        exclude_lead_id: int | None = None,
    ) -> MatchResult | None:
        """Return the best strong/medium match among existing leads, or None.

        Only *candidates* are loaded (FIX-12): leads sharing a normalized key with
        the new one (phone suffix / website host / social handle) or sitting in the
        same city. Everything else is filtered out by SQL, so the in-memory
        comparison is proportional to the number of plausible duplicates instead of
        the whole base. ``classify_match`` still makes the final, exact decision —
        the candidate query may only ever be *broader*, never narrower.
        """
        leads = await self._load_candidates(fingerprint, owner_user_id, exclude_lead_id)
        if not leads:
            # The keyed query found nothing. A city stored in a spelling the SQL
            # variants do not cover (a copy-paste «Алматы  қаласы») would silently
            # drop a *medium* match, so fall back to a bounded recency scan —
            # explicitly logged, never a full-table load.
            leads = await self._load_recent_fallback(
                fingerprint, owner_user_id, exclude_lead_id
            )

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

    async def _fetch(self, stmt) -> list[Lead]:
        async with self.session_factory() as session:
            result = await session.execute(stmt)
            return list(result.scalars().all())

    def _candidate_query(self, fingerprint: LeadFingerprint, owner_user_id: int,
                         exclude_lead_id: int | None):
        """Bounded SELECT of plausible duplicates, or None when the new lead has no key."""
        strong_conditions = _strong_conditions(fingerprint)
        conditions = strong_conditions + _city_conditions(fingerprint.city)
        if not conditions:
            return None

        stmt = select(Lead).where(
            Lead.owner_user_id == owner_user_id,
            Lead.deleted_at.is_(None),
            or_(*conditions),
        )
        if exclude_lead_id is not None:
            stmt = stmt.where(Lead.id != exclude_lead_id)
        # Hard identifiers first: if the limit truncates the list, the rows that
        # could be a *strong* match must not be the ones dropped.
        if strong_conditions:
            stmt = stmt.order_by(
                case((or_(*strong_conditions), 0), else_=1), Lead.id.desc()
            )
        else:
            stmt = stmt.order_by(Lead.id.desc())
        return stmt.limit(self.candidate_limit)

    async def _load_candidates(self, fingerprint, owner_user_id, exclude_lead_id) -> list[Lead]:
        stmt = self._candidate_query(fingerprint, owner_user_id, exclude_lead_id)
        if stmt is None:
            return []
        leads = await self._fetch(stmt)
        self._warn_if_truncated(leads, "dedup_candidate_limit")
        return leads

    async def _load_recent_fallback(self, fingerprint, owner_user_id, exclude_lead_id) -> list[Lead]:
        """Bounded scan used only when the keyed query matched nothing.

        A medium match needs a fuzzy name *and* a city, so without a name there is
        nothing this fallback could find — it stays off in that case.
        """
        if not (fingerprint.name_key and fingerprint.city):
            return []
        stmt = (
            select(Lead)
            .where(
                Lead.owner_user_id == owner_user_id,
                Lead.deleted_at.is_(None),
            )
            .order_by(Lead.id.desc())
            .limit(self.candidate_limit)
        )
        if exclude_lead_id is not None:
            stmt = stmt.where(Lead.id != exclude_lead_id)
        leads = await self._fetch(stmt)
        if leads:
            # Only worth a line when something was actually compared: on an empty
            # base (the first lead ever) nothing could have been missed.
            log_json(
                logger, 30, "dedup keyed lookup found nothing — bounded recency scan used",
                action="dedup_fallback_scan", limit=self.candidate_limit,
                compared=len(leads), owner_user_id=owner_user_id,
            )
            self._warn_if_truncated(leads, "dedup_fallback_limit")
        return leads

    def _warn_if_truncated(self, leads: list[Lead], action: str) -> None:
        if len(leads) >= self.candidate_limit:
            log_json(
                logger, 30, "dedup candidate limit reached — some leads were not compared",
                action=action, limit=self.candidate_limit, compared=len(leads),
            )


def _strong_conditions(fingerprint: LeadFingerprint) -> list:
    """SQL predicates for the hard identifiers (all stored normalized)."""
    conditions = []
    for suffix in _phone_suffixes(fingerprint.phone_digits):
        conditions.append(
            Lead.phone.like(f"%{escape_like(suffix)}", escape=LIKE_ESCAPE)
        )
    website_host = (fingerprint.website or "").split("/")[0]
    if website_host:
        conditions.append(
            Lead.website.like(f"%{escape_like(website_host)}%", escape=LIKE_ESCAPE)
        )
    # Instagram/Telegram handles are ASCII and stored lowercased, so ``lower()``
    # (ASCII-only in SQLite) is exact here.
    if fingerprint.instagram:
        conditions.append(func.lower(Lead.instagram) == fingerprint.instagram)
    if fingerprint.telegram:
        conditions.append(func.lower(Lead.telegram) == fingerprint.telegram)
    return conditions


def _city_conditions(city_key: str | None) -> list:
    """SQL predicates matching leads of the same city.

    ``lower()`` in SQLite folds ASCII only, so ``lower('Алматы')`` stays «Алматы»
    and a ``lower(city) = 'алматы'`` filter silently matched *nothing* for every
    Cyrillic city. The stored spelling is raw (whatever the source said), so the
    normalized key is compared against its usual spellings instead; the candidate
    set may be broader than needed, which is free — ``classify_match`` decides.
    """
    if not city_key:
        return []
    stored = func.replace(func.replace(func.trim(Lead.city), ",", ""), ".", "")
    variants = {city_key, city_key.capitalize(), city_key.title(), city_key.upper()}
    return [stored == variant for variant in sorted(variants)]


def _phone_suffixes(phone_digits_value: str | None) -> list[str]:
    """Suffix lengths the phone comparison can match on, longest first.

    10 and 9 digits feed the strong rule; 7 digits feed the medium rule (same last
    7 digits with a different full number) — the candidate query must not lose it.
    """
    if not phone_digits_value:
        return []
    suffixes: list[str] = []
    for length in (10, 9, 7):
        if len(phone_digits_value) >= length:
            suffix = phone_digits_value[-length:]
            if suffix not in suffixes:
                suffixes.append(suffix)
    return suffixes


def _level_rank(level: MatchLevel) -> int:
    return {"none": 0, "medium": 1, "strong": 2}[level]
