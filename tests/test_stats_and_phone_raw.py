"""FIX-11: cost/token accounting in /stats + the raw phone spelling in the database.

Two audit findings are covered here:

* ``phone_raw`` was extracted (and promised by the prompt, ТЗ §7 «исходное написание
  для аудита») but never stored — the normalization could not be audited afterwards.
* every ``:free`` extraction was logged as ``cost_usd_est = 0.0``, so «расход на AI»
  could not show a real number, and the *tokens* that do describe the load were never
  reported at all. A zero must now be labelled as a free model instead of being
  presented as a measured spend.
"""
from __future__ import annotations

import pytest

from app.models import ExtractionLog
from app.schemas.extraction import ExtractionResult
from app.services.extraction import (
    FALLBACK_MODEL,
    PRIMARY_MODEL,
    ExtractionService,
    is_free_model,
)
from app.services.lead_service import LeadService, describe_cost


class _FakeSessionFactory:
    """Session factory stub — ``_estimate_cost`` is pure, nothing is persisted."""

    def __call__(self):
        raise AssertionError("no database access expected in this test")


# ---------------- phone_raw is stored (ТЗ §7) ----------------
@pytest.mark.asyncio
async def test_add_lead_persists_phone_raw_next_to_the_normalized_number(session_factory):
    svc = LeadService(session_factory)
    lead = await svc.add_lead(
        1,
        ExtractionResult(
            company_name="Ali Motors",
            phone_raw="+7 (700) 123-45-67",
            phone_e164="+77001234567",
        ),
    )

    stored = await svc.get_lead(lead.id)
    assert stored.phone_raw == "+7 (700) 123-45-67", "the source spelling must survive"
    assert stored.phone == "+77001234567", "the normalized number is unchanged"


@pytest.mark.asyncio
async def test_phone_raw_is_normalized_from_when_only_the_raw_form_is_known(session_factory):
    """The raw spelling alone must still produce a comparable E.164 number."""
    svc = LeadService(session_factory)
    lead = await svc.add_lead(1, ExtractionResult(company_name="Ali", phone_raw="8 700 123 45 67"))

    stored = await svc.get_lead(lead.id)
    assert stored.phone_raw == "8 700 123 45 67"
    assert stored.phone == "+77001234567"


@pytest.mark.asyncio
async def test_phone_raw_stays_null_when_the_source_had_no_phone(session_factory):
    svc = LeadService(session_factory)
    lead = await svc.add_lead(1, ExtractionResult(company_name="Ali"))

    assert (await svc.get_lead(lead.id)).phone_raw is None


@pytest.mark.asyncio
async def test_merge_fills_phone_raw_of_the_older_row(session_factory):
    """FIX-11 data is mergeable like every other business field."""
    svc = LeadService(session_factory)
    existing = await svc.add_lead(
        1, ExtractionResult(company_name="Ali", phone_e164="+77001234567")
    )
    assert existing.phone_raw is None

    await svc.add_lead(
        1,
        ExtractionResult(
            company_name="Ali", phone_raw="+7 700 123 45 67", phone_e164="+77001234567"
        ),
        merge_target_id=existing.id,
    )

    stored = await svc.get_lead(existing.id)
    assert stored.phone_raw == "+7 700 123 45 67"
    assert stored.phone == "+77001234567"


# ---------------- get_stats aggregation ----------------
async def _log_extractions(session_factory, session_id: int, rows: list[dict]) -> None:
    async with session_factory() as session:
        for row in rows:
            session.add(ExtractionLog(session_id=session_id, **row))
        await session.commit()


@pytest.mark.asyncio
async def test_get_stats_aggregates_extractions_tokens_cost_and_duplicates(session_factory):
    svc = LeadService(session_factory)
    session_id = await svc.create_session(7)
    await _log_extractions(
        session_factory,
        session_id,
        [
            {"model": "openrouter/free", "tokens_in": 100, "tokens_out": 20,
             "cost_usd_est": 0.0, "latency_ms": 900, "success": True},
            {"model": "openrouter/free", "tokens_in": 50, "tokens_out": 10,
             "cost_usd_est": 0.0, "latency_ms": 800, "success": False, "error": "bad json"},
            {"model": "google/gemini-2.0-flash-001", "tokens_in": 10, "tokens_out": 5,
             "cost_usd_est": 0.000125, "latency_ms": 700, "success": True},
        ],
    )
    await svc.add_lead(7, ExtractionResult(company_name="Ромашка"))
    live = await svc.add_lead(
        7, ExtractionResult(company_name="Ромашка", phone_e164="+77001234567")
    )
    await svc.add_lead(
        7,
        ExtractionResult(company_name="Ромашка", phone_e164="+77001234567"),
        merge_target_id=live.id,
    )

    stats = await svc.get_stats(7)

    assert stats["total"] == 2, "the merged duplicate row is not a separate lead"
    assert stats["week"] == 2
    assert stats["duplicates"] == 1
    assert stats["extractions"] == 3
    assert stats["extractions_ok"] == 2, "a failed attempt is still an attempt"
    assert stats["tokens_in"] == 160
    assert stats["tokens_out"] == 35
    assert stats["cost_usd"] == pytest.approx(0.000125)
    assert stats["free_extractions"] == 2


@pytest.mark.asyncio
async def test_get_stats_is_scoped_to_the_owner(session_factory):
    """Another owner's extraction logs must not appear in this owner's spend."""
    svc = LeadService(session_factory)
    mine = await svc.create_session(7)
    theirs = await svc.create_session(999)
    await _log_extractions(
        session_factory, mine, [{"model": "m:free", "tokens_in": 10, "tokens_out": 1,
                                 "cost_usd_est": 0.0, "success": True}]
    )
    await _log_extractions(
        session_factory, theirs, [{"model": "paid", "tokens_in": 1000, "tokens_out": 500,
                                   "cost_usd_est": 9.5, "success": True}]
    )

    stats = await svc.get_stats(7)

    assert stats["extractions"] == 1
    assert stats["tokens_in"] == 10
    assert stats["cost_usd"] == 0.0


@pytest.mark.asyncio
async def test_get_stats_reports_zeros_when_nothing_was_extracted(session_factory):
    svc = LeadService(session_factory)

    stats = await svc.get_stats(7)

    assert stats["extractions"] == 0
    assert stats["tokens_in"] == 0 and stats["tokens_out"] == 0
    assert stats["cost_usd"] == 0.0


@pytest.mark.asyncio
async def test_get_stats_tolerates_null_tokens_and_cost(session_factory):
    """A provider that answered without ``usage`` must not break the sum."""
    svc = LeadService(session_factory)
    session_id = await svc.create_session(7)
    await _log_extractions(
        session_factory,
        session_id,
        [{"model": "unknown", "tokens_in": None, "tokens_out": None,
          "cost_usd_est": None, "success": False, "error": "network"}],
    )

    stats = await svc.get_stats(7)

    assert stats["extractions"] == 1
    assert stats["tokens_in"] == 0 and stats["tokens_out"] == 0
    assert stats["cost_usd"] == 0.0


# ---------------- the wording must never invent a spend ----------------
def test_describe_cost_names_a_free_model():
    assert describe_cost(0.0, extractions=5, free_extractions=5) == "$0 (бесплатная модель)"


def test_describe_cost_shows_the_real_amount():
    assert describe_cost(0.001234, extractions=3, free_extractions=0) == "$0.001234"


def test_describe_cost_does_not_call_an_unpriced_paid_model_free():
    assert describe_cost(0.0, extractions=3, free_extractions=2) == "$0 (тариф модели не учтён)"


def test_describe_cost_says_so_when_there_were_no_calls():
    assert "нет данных" in describe_cost(0.0, extractions=0, free_extractions=0)


# ---------------- provider-reported cost wins over the :free shortcut ----------------
def test_estimate_cost_prefers_the_provider_reported_cost():
    """``usage.cost`` is the only measured number; the table is a fallback."""
    cost = ExtractionService._estimate_cost(
        "google/gemini-2.0-flash-001", 1000, 500, {"cost": 0.00042}
    )
    assert cost == pytest.approx(0.00042)


def test_estimate_cost_uses_the_price_table_when_the_provider_gives_no_cost():
    cost = ExtractionService._estimate_cost("openai/gpt-4o-mini", 1_000_000, 0, {})
    assert cost == pytest.approx(0.15)


def test_estimate_cost_of_a_free_model_is_still_zero():
    """``:free`` stays the fallback for providers that omit ``usage.cost``."""
    assert ExtractionService._estimate_cost(FALLBACK_MODEL, 100, 10, {}) == 0.0


def test_estimate_cost_of_an_unknown_paid_model_is_none_not_zero():
    """No price known ⇒ None («тариф не учтён»), never an invented 0."""
    assert ExtractionService._estimate_cost("some/unknown-model", 100, 10, {}) is None


def test_estimate_cost_uses_a_provider_reported_zero_for_a_free_model():
    """A reported 0 is kept as a *reported* 0 — the branch is a fallback only."""
    assert ExtractionService._estimate_cost("x:free", 10, 5, {"cost": 0.0}) == 0.0
    assert ExtractionService._estimate_cost("x:free", 10, 5, {"cost": "0"}) == 0.0


def test_free_model_detection_covers_the_router_alias_and_the_suffix():
    """The default primary model is ``openrouter/free`` — no ``:free`` suffix."""
    assert is_free_model(PRIMARY_MODEL) is True
    assert is_free_model(FALLBACK_MODEL) is True
    assert is_free_model("openai/gpt-4o-mini") is False
    assert is_free_model(None) is False


def test_estimate_cost_of_the_default_router_alias_is_zero():
    assert ExtractionService._estimate_cost(PRIMARY_MODEL, 100, 10, {}) == 0.0


@pytest.mark.asyncio
async def test_get_stats_counts_the_default_free_model_as_free(session_factory):
    """The free-model counter must recognise the model the bot actually uses."""
    svc = LeadService(session_factory)
    session_id = await svc.create_session(7)
    await _log_extractions(
        session_factory,
        session_id,
        [{"model": PRIMARY_MODEL, "tokens_in": 700, "tokens_out": 200,
          "cost_usd_est": 0.0, "success": True}],
    )

    stats = await svc.get_stats(7)

    assert stats["free_extractions"] == 1
    assert describe_cost(
        stats["cost_usd"], stats["extractions"], stats["free_extractions"]
    ) == "$0 (бесплатная модель)"
