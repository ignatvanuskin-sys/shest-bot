"""FSM + session buffer tests (buffer, /done before timeout, /cancel, manual fallback)."""
from __future__ import annotations

import asyncio

import pytest

from app.bot import flow
from app.bot.states import LeadForm
from app.schemas.extraction import ExtractionResult
from app.services.dedup import MatchResult
from app.services.extraction import ExtractionError
from app.services.session_buffer import SessionBufferService
from tests.conftest import FakeContainer, FakeDedup, FakeExtraction, make_fsm


# ---------------- session buffer ----------------
@pytest.mark.asyncio
async def test_session_buffer_fires_after_timeout():
    buf = SessionBufferService(timeout_seconds=0.1)
    fired = []

    async def cb():
        fired.append(1)

    buf.schedule(1, cb)
    await asyncio.sleep(0.25)
    assert fired == [1]
    assert not buf.has_pending(1)


@pytest.mark.asyncio
async def test_session_buffer_cancel_prevents_fire():
    buf = SessionBufferService(timeout_seconds=0.1)
    fired = []

    async def cb():
        fired.append(1)

    buf.schedule(1, cb)
    buf.cancel(1)
    await asyncio.sleep(0.25)
    assert fired == []


@pytest.mark.asyncio
async def test_session_buffer_reschedule_resets_timer():
    buf = SessionBufferService(timeout_seconds=0.2)
    fired = []

    async def cb():
        fired.append(1)

    buf.schedule(1, cb)
    await asyncio.sleep(0.05)
    buf.schedule(1, cb)  # new message resets the timer
    await asyncio.sleep(0.05)
    buf.cancel(1)
    await asyncio.sleep(0.3)
    assert fired == []


# ---------------- flow transitions ----------------
def _extracted() -> ExtractionResult:
    return ExtractionResult(company_name="Ali Motors", city="Караганда", phone_e164="+77001234567")


@pytest.mark.asyncio
async def test_finalize_routes_to_review_without_duplicate():
    container = FakeContainer(extraction=FakeExtraction(result=_extracted()), dedup=FakeDedup(match=None))
    storage, state = make_fsm()
    await state.set_state(LeadForm.Collecting)
    await state.set_data({"session_id": 1, "combined_text": "Ali Motors Караганда"})

    await flow.finalize_collection(container, 1, 1, state)

    assert await state.get_state() == LeadForm.Reviewing.state
    data = await state.get_data()
    assert data["extracted"]["company_name"] == "Ali Motors"
    assert container.bot.sent, "bot should send the review card"


@pytest.mark.asyncio
async def test_finalize_routes_to_duplicate_confirmation():
    match = MatchResult("medium", "похожее название в одном городе", score=90, lead_id=5)
    container = FakeContainer(extraction=FakeExtraction(result=_extracted()), dedup=FakeDedup(match=match))
    storage, state = make_fsm()
    await state.set_state(LeadForm.Collecting)
    await state.set_data({"session_id": 1, "combined_text": "Ali Motors Караганда"})

    await flow.finalize_collection(container, 1, 1, state)

    assert await state.get_state() == LeadForm.ConfirmingDuplicate.state
    data = await state.get_data()
    assert data["duplicate"]["lead_id"] == 5


@pytest.mark.asyncio
async def test_finalize_manual_fallback_on_llm_error():
    container = FakeContainer(extraction=FakeExtraction(result=ExtractionError("boom")))
    storage, state = make_fsm()
    await state.set_state(LeadForm.Collecting)
    await state.set_data({"session_id": 1, "combined_text": "text"})

    await flow.finalize_collection(container, 1, 1, state)

    assert await state.get_state() == LeadForm.ManualEntry.state


@pytest.mark.asyncio
async def test_finalize_manual_fallback_without_api_key():
    container = FakeContainer(extraction=FakeExtraction(result=_extracted(), api_key=""))
    storage, state = make_fsm()
    await state.set_state(LeadForm.Collecting)
    await state.set_data({"session_id": 1, "combined_text": "text"})

    await flow.finalize_collection(container, 1, 1, state)

    assert await state.get_state() == LeadForm.ManualEntry.state
    assert any("LLM не настроен" in t for _, t, _ in container.bot.sent)


@pytest.mark.asyncio
async def test_cancel_clears_state():
    container = FakeContainer()
    storage, state = make_fsm()
    await state.set_state(LeadForm.Collecting)
    await state.set_data({"session_id": 1, "combined_text": "text"})

    await flow.cancel_collection(container, 1, state, chat_id=1)

    assert await state.get_state() is None


@pytest.mark.asyncio
async def test_done_finalizes_before_timer():
    container = FakeContainer(extraction=FakeExtraction(result=_extracted()), dedup=FakeDedup(match=None))
    storage, state = make_fsm()
    await state.set_state(LeadForm.Collecting)
    await state.set_data({"session_id": 1, "combined_text": "Ali"})

    # A timer is pending, but /done (finalize) fires immediately and cancels it.
    container.session_buffer.schedule(
        1, lambda: flow.finalize_collection(container, 1, 1, state)
    )
    assert container.session_buffer.has_pending(1)

    await flow.finalize_collection(container, 1, 1, state)

    assert await state.get_state() == LeadForm.Reviewing.state
    assert not container.session_buffer.has_pending(1)
