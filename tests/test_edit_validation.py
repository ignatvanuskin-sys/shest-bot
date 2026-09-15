"""FIX-3 regression: editing a field must validate the value, never crash.

Production symptom: ``on_edit_text`` put the raw message text into the extracted
data and validated the whole model afterwards. A non-numeric «Рейтинг» («нет
данных») or an invalid «Источник» raised ``ValidationError`` inside the handler —
the user got *no* reply at all and the dialog stayed in ``EditingField`` forever.

The contract under test:

* ``flow.apply_edit`` validates the candidate against ``ExtractionResult`` and
  raises ``EditValidationError`` instead of letting pydantic through;
* the handler answers with a human hint, keeps the field selected and keeps the
  previously extracted data untouched;
* a valid value still goes through (card shown again, lead saved with it).
"""
from __future__ import annotations

import pytest

from app.bot import flow
from app.bot.keyboards import CB_ADD, CB_EDIT
from app.bot.states import LeadForm
from app.schemas.extraction import ExtractionResult
from tests.integration_harness import (
    assert_valid_telegram_html,
    harness,  # noqa: F401  — imported fixture
    no_network,  # noqa: F401  — autouse imported fixture (blocks non-loopback sockets)
)


def full_result() -> ExtractionResult:
    return ExtractionResult(
        company_name="Ромашка",
        city="Алматы",
        phone_raw="+7 700 123 45 67",
        phone_e164="+77001234567",
        source_guess="2gis",
    )


async def _open_field(harness, field: str) -> None:
    """Review card → «Исправить» → pick *field* (the state the bug stuck in)."""
    await harness.tap(CB_EDIT)
    await harness.tap(f"field:{field}")
    assert await harness.fsm_state() == LeadForm.EditingField.state
    assert (await harness.fsm_data())["editing_field"] == field


# ---------------- unit level ----------------
def test_apply_edit_rejects_a_non_numeric_rating():
    with pytest.raises(flow.EditValidationError) as exc:
        flow.apply_edit({"rating": None}, "rating", "нет данных")
    assert exc.value.field == "rating"
    assert "число" in exc.value.hint


def test_apply_edit_rejects_an_unknown_source():
    with pytest.raises(flow.EditValidationError):
        flow.apply_edit({"source_guess": None}, "source_guess", "авито")


def test_apply_edit_rejects_a_field_that_is_not_editable():
    """Callback data is user-controlled: unknown keys must not reach the schema."""
    with pytest.raises(flow.EditValidationError) as exc:
        flow.apply_edit({}, "owner_user_id", "42")
    assert "неизвестное" in exc.value.hint


def test_apply_edit_accepts_and_normalizes_valid_values():
    data = {"phone_raw": None, "phone_e164": None, "instagram": None, "services": []}
    data = flow.apply_edit(data, "phone_raw", "+7 700 123 45 67")
    assert data["phone_e164"] == "+77001234567"
    data = flow.apply_edit(data, "instagram", "@romashka")
    assert data["instagram"] == "romashka"
    data = flow.apply_edit(data, "services", "мойка, шиномонтаж")
    assert data["services"] == ["мойка", "шиномонтаж"]
    # The value comes back canonical (schema types), not as the raw string.
    assert flow.apply_edit(data, "rating", "4.8")["rating"] == 4.8
    assert flow.apply_edit(data, "reviews_count", "120")["reviews_count"] == 120
    assert flow.apply_edit(data, "source_guess", "")["source_guess"] is None


def test_apply_edit_leaves_the_original_data_untouched():
    original = {"company_name": "Ромашка", "rating": 4.8}
    flow.apply_edit(original, "company_name", "Другая")
    assert original == {"company_name": "Ромашка", "rating": 4.8}


# ---------------- through the real dispatcher ----------------
async def test_invalid_rating_keeps_the_dialog_and_the_data(harness):
    harness.extraction._results = [full_result()]
    await harness.send_text("ТОО Ромашка, Алматы, +7 700 123 45 67")
    await harness.send_command("/done")
    await _open_field(harness, "rating")

    await harness.send_text("нет данных")  # the exact input from the audit

    assert harness.bot.contains("нужно число"), "the user got no explanation"
    assert harness.bot.contains("Для «Рейтинг»"), "the reply does not name the field"
    assert_valid_telegram_html(harness.bot.last_message().text)
    assert await harness.fsm_state() == LeadForm.EditingField.state, "dialog got stuck/lost"
    data = await harness.fsm_data()
    assert data["editing_field"] == "rating", "the field is no longer selected"
    assert data["extracted"]["rating"] is None, "the bad value leaked into the data"
    assert data["extracted"]["company_name"] == "Ромашка", "previously edited data was lost"

    # The user can simply answer again — with a valid number this time.
    await harness.send_text("4.8")

    assert await harness.fsm_state() == LeadForm.Reviewing.state
    assert (await harness.fsm_data())["extracted"]["rating"] == 4.8
    assert harness.bot.contains("4.8")

    await harness.tap(CB_ADD)
    leads = await harness.leads()
    assert len(leads) == 1
    assert leads[0].rating == 4.8
    assert leads[0].company_name == "Ромашка"


async def test_invalid_source_is_refused_and_a_valid_one_is_accepted(harness):
    harness.extraction._results = [full_result()]
    await harness.send_text("ТОО Ромашка, Алматы, +7 700 123 45 67")
    await harness.send_command("/done")
    await _open_field(harness, "source_guess")

    await harness.send_text("авито")
    assert harness.bot.contains("допустимо: 2gis")
    assert await harness.fsm_state() == LeadForm.EditingField.state
    assert (await harness.fsm_data())["extracted"]["source_guess"] == "2gis"

    await harness.send_text("instagram")
    assert await harness.fsm_state() == LeadForm.Reviewing.state
    await harness.tap(CB_ADD)
    assert (await harness.leads())[0].source == "instagram"


async def test_invalid_edit_does_not_disturb_the_earlier_valid_edit(harness):
    """A rejected value must not throw away the field edited a moment before."""
    harness.extraction._results = [full_result()]
    await harness.send_text("ТОО Ромашка, Алматы, +7 700 123 45 67")
    await harness.send_command("/done")

    await _open_field(harness, "city")
    await harness.send_text("Караганда")
    assert (await harness.fsm_data())["extracted"]["city"] == "Караганда"

    await _open_field(harness, "rating")
    await harness.send_text("очень высокий")

    assert harness.bot.contains("нужно число")
    data = await harness.fsm_data()
    assert data["extracted"]["city"] == "Караганда"
    assert data["extracted"]["rating"] is None

    # «Готово» in the fields keyboard returns to the review card with the good data.
    await harness.tap("edit_done")
    assert await harness.fsm_state() == LeadForm.Reviewing.state
    await harness.tap(CB_ADD)
    assert (await harness.leads())[0].city == "Караганда"


async def test_edit_without_a_selected_field_asks_for_one(harness):
    harness.extraction._results = [full_result()]
    await harness.send_text("ТОО Ромашка, Алматы, +7 700 123 45 67")
    await harness.send_command("/done")
    await harness.tap(CB_EDIT)

    await harness.send_text("просто текст")

    assert harness.bot.contains("Сначала выберите поле")
    assert await harness.fsm_state() == LeadForm.EditingField.state
