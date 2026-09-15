"""FIX-1 regression: exactly one extraction per finalized buffer, one card, one lead.

The production symptom: ``finalize_collection`` left the FSM in ``Collecting`` while
the LLM was working (15–40 s on the free models). A message sent in that window ran
a *second* extraction for the same buffer → two cards in the chat, and «Добавить»
under the first card saved the data of the second lead.

The contract under test:

* the dialog moves to ``LeadForm.Processing`` *before* the LLM call;
* a message that arrives while processing is answered («уже обрабатываю») and never
  reaches the extraction service;
* a second concurrent ``finalize_collection`` (timer + /done, double tap) extracts
  nothing — the in-flight claim closes the window between ``get_state``/``set_state``;
* the state comes back to Idle / Next-step on every path (success, LLM error,
  /cancel mid-processing, unexpected exception).

Everything runs through the real dispatcher (``tests.integration_harness``): the only
fakes are the recording bot, the scripted extraction service and the sheets backend.
"""
from __future__ import annotations

import asyncio

import pytest
from aiogram.enums import ParseMode

from app.bot import flow
from app.bot.keyboards import CB_ADD, CB_CANCEL
from app.bot.states import LeadForm
from app.schemas.extraction import ExtractionResult
from tests.integration_harness import (
    assert_valid_telegram_html,
    harness,  # noqa: F401  — imported fixture
    no_network,  # noqa: F401  — autouse imported fixture (blocks non-loopback sockets)
    wait_until,
)

FIRST_TEXT = "ТОО Ромашка, Алматы, +7 700 123 45 67"
# A second, deliberately different message: if it ever got extracted, the saved lead
# would carry this company/phone instead of «Ромашка».
SECOND_TEXT = "Другая Компания, Павлодар, +7 701 000 00 00"


def first_result() -> ExtractionResult:
    return ExtractionResult(
        company_name="Ромашка",
        city="Алматы",
        phone_raw="+7 700 123 45 67",
        phone_e164="+77001234567",
        instagram="romashka",
        source_guess="2gis",
    )


class GatedExtraction:
    """Extraction service that blocks until the test releases it.

    ``started`` fires when the pipeline has entered the LLM call, which is exactly
    the production window in which the user kept typing.
    """

    def __init__(self, results: list[ExtractionResult], *, api_key: str = "test-openrouter-key"):
        self.api_key = api_key
        self._results = list(results)
        self.calls: list[str] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def extract(self, text, session_id=None):
        self.calls.append(text)
        self.started.set()
        await self.release.wait()
        if not self._results:
            raise AssertionError("GatedExtraction has no scripted result left")
        return self._results.pop(0)

    async def close(self):
        return None


class BoomExtraction:
    """Extraction service that fails with an unexpected (non-ExtractionError) error."""

    api_key = "test-openrouter-key"

    async def extract(self, text, session_id=None):
        raise RuntimeError("unexpected pipeline failure")

    async def close(self):
        return None


@pytest.fixture(autouse=True)
def clear_finalize_claim():
    """Keep the in-process claim set from leaking between tests."""
    flow._FINALIZE_IN_FLIGHT.clear()
    yield
    flow._FINALIZE_IN_FLIGHT.clear()


def review_cards(harness) -> list:
    """Messages that carry the «Добавить» button — i.e. rendered review cards."""
    cards = []
    for call in harness.bot.messages():
        markup = call.reply_markup
        if markup is None:
            continue
        data = {
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
            if button.callback_data
        }
        if CB_ADD in data:
            cards.append(call)
    return cards


# ---------------- the reported race ----------------
async def test_message_during_extraction_never_starts_a_second_extraction(harness):
    gated = GatedExtraction([first_result()])
    harness.container.extraction = gated

    await harness.send_text(FIRST_TEXT)
    finalize = asyncio.create_task(harness.send_command("/done"))
    try:
        assert await wait_until(lambda: gated.started.is_set()), "extraction never started"

        # The dialog is locked *before* the slow call: this message must not extract.
        await harness.send_text(SECOND_TEXT)

        assert harness.bot.contains("Уже обрабатываю предыдущее сообщение")
        busy_reply = harness.bot.last_message()
        assert busy_reply.parse_mode == ParseMode.HTML, "premium emoji needs HTML"
        assert_valid_telegram_html(busy_reply.text)
        assert await harness.fsm_state() == LeadForm.Processing.state
        assert len(gated.calls) == 1, "a second extraction started while the first was running"
    finally:
        gated.release.set()
        await finalize

    # Exactly one extraction, exactly one card.
    assert len(gated.calls) == 1
    assert gated.calls == [FIRST_TEXT], "the rejected message must not reach the LLM"
    assert await harness.fsm_state() == LeadForm.Reviewing.state
    assert len(review_cards(harness)) == 1, "a second card was sent for the same buffer"

    # ...and «Добавить» under that card saves *that* lead.
    await harness.tap(CB_ADD)
    leads = await harness.leads()
    assert len(leads) == 1
    assert leads[0].company_name == "Ромашка"
    assert leads[0].phone == "+77001234567"
    assert "Другая Компания" not in (leads[0].company_name or "")


async def test_claim_blocks_the_second_caller_before_the_state_is_set(harness):
    """The FSM alone is not a lock: get_state/set_state are two awaits.

    ``finalize_collection`` must refuse to run when another caller already claimed
    the user, even if the state is still ``Collecting`` (the exact interleaving that
    produced two cards in production).
    """
    gated = GatedExtraction([first_result()])
    harness.container.extraction = gated

    await harness.send_text(FIRST_TEXT)
    assert flow._claim_finalize(424242) is True  # stand-in for the racing caller
    try:
        await harness.send_command("/done")

        assert gated.calls == [], "the claim did not prevent the second extraction"
        assert harness.bot.contains("Уже обрабатываю предыдущее сообщение")
        assert await harness.fsm_state() == LeadForm.Collecting.state, "state was hijacked"
    finally:
        flow._release_finalize(424242)

    # The claim is released, so the normal path still works afterwards.
    finalize = asyncio.create_task(harness.send_command("/done"))
    assert await wait_until(lambda: gated.started.is_set())
    gated.release.set()
    await finalize

    assert len(gated.calls) == 1
    assert await harness.fsm_state() == LeadForm.Reviewing.state
    assert len(review_cards(harness)) == 1


async def test_double_done_does_not_extract_twice(harness):
    gated = GatedExtraction([first_result()])
    harness.container.extraction = gated

    await harness.send_text(FIRST_TEXT)
    first = asyncio.create_task(harness.send_command("/done"))
    try:
        assert await wait_until(lambda: gated.started.is_set())
        await harness.send_command("/done")  # user taps «Готово» again
    finally:
        gated.release.set()
        await first

    assert len(gated.calls) == 1
    assert len(review_cards(harness)) == 1
    assert await harness.fsm_state() == LeadForm.Reviewing.state


# ---------------- every exit path returns to a sane state ----------------
async def test_cancel_while_processing_leaves_no_card_and_no_lead(harness):
    gated = GatedExtraction([first_result()])
    harness.container.extraction = gated

    await harness.send_text(FIRST_TEXT)
    finalize = asyncio.create_task(harness.send_command("/done"))
    try:
        assert await wait_until(lambda: gated.started.is_set())
        await harness.send_command("/cancel")
        assert harness.bot.contains("Отменено")
    finally:
        gated.release.set()
        await finalize

    # The stale pipeline must not talk to the chat after the cancel.
    assert await harness.fsm_state() is None, "the cancelled dialog was re-opened"
    assert review_cards(harness) == [], "a card appeared after /cancel"
    assert await harness.lead_count() == 0
    sessions = await harness.sessions()
    assert [session.status for session in sessions] == ["cancelled"]
    assert sessions[0].resulting_lead_id is None


async def test_new_lead_while_processing_is_not_hijacked(harness):
    gated = GatedExtraction([first_result()])
    harness.container.extraction = gated

    await harness.send_text(FIRST_TEXT)
    finalize = asyncio.create_task(harness.send_command("/done"))
    try:
        assert await wait_until(lambda: gated.started.is_set())
        await harness.send_command("/new")
        assert await harness.fsm_state() == LeadForm.Collecting.state
    finally:
        gated.release.set()
        await finalize

    # The finished (stale) pipeline must not overwrite the new collection.
    assert await harness.fsm_state() == LeadForm.Collecting.state
    assert review_cards(harness) == []
    assert await harness.lead_count() == 0


async def test_unexpected_error_returns_the_dialog_to_idle(harness):
    harness.container.extraction = BoomExtraction()

    await harness.send_text(FIRST_TEXT)
    await harness.send_command("/done")  # must not raise

    assert await harness.fsm_state() is None, "the user stayed stuck in «обработка»"
    assert harness.bot.contains("Не получилось обработать сообщение")
    assert await harness.lead_count() == 0
    assert flow.is_finalizing(424242) is False, "the claim leaked"


async def test_cancel_button_during_collecting_still_works(harness):
    """The collecting keyboard keeps working while nothing is in flight."""
    await harness.send_text(FIRST_TEXT)
    assert await harness.fsm_state() == LeadForm.Collecting.state

    await harness.tap(CB_CANCEL)

    assert await harness.fsm_state() is None
    assert harness.bot.contains("Отменено")
