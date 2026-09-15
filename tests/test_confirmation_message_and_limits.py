"""FIX-24 (one confirmation message) + FIX-25b/c (list limits).

FIX-24: «Лид добавлен — ID #7» and «Строка 12 готова» arrived as two messages in a
row. Both facts belong to one confirmation, so the sync job — the first moment the
row number exists — sends one message, with the table link when one is configured,
and falls back to one deferred notice when the row could not be written.

FIX-25: the list commands were the last hardcoded numbers in the handlers: /last
always showed 5 leads and /search always returned an arbitrary 10 in ID order. Both
limits now come from the environment and the search is newest-first.
"""
from __future__ import annotations

import pytest

from app.bot.handlers import DEFAULT_LAST_LEADS_LIMIT, DEFAULT_SEARCH_RESULT_LIMIT
from app.bot.keyboards import CB_ADD, CB_DUP_NEW
from app.schemas.extraction import ExtractionResult
from app.services.lead_service import LeadService
from tests.integration_harness import (
    harness,  # noqa: F401  — imported fixture
    no_network,  # noqa: F401  — autouse imported fixture (blocks non-loopback sockets)
    wait_until,
)

# Distinct names *and* phones: identical contact data would be a duplicate (strong
# merge) and identical names in one city would raise the «похоже на дубль» question,
# while these tests need several independent leads.
NAMES = ("Альфа", "Бета", "Гамма", "Дельта", "Эпсилон", "Дзета", "Эта")


def full_result(name: str = "Ромашка") -> ExtractionResult:
    return ExtractionResult(
        company_name=name,
        city="Алматы",
        phone_raw="+7 700 123 45 67",
        phone_e164="+77001234567",
        source_guess="2gis",
    )


def distinct_result(index: int) -> ExtractionResult:
    """The *index*-th unrelated lead (no shared phone, no city → never a duplicate)."""
    return ExtractionResult(
        company_name=f"Ромашка {NAMES[index]}",
        phone_e164=f"+7700000{index:04d}",
    )


async def _save_one_lead(harness, name: str = "Ромашка") -> None:
    harness.extraction._results = [full_result(name)]
    await harness.send_text(f"ТОО {name}, Алматы, +7 700 123 45 67")
    await harness.send_command("/done")
    await harness.tap(CB_ADD)
    lead_id = (await harness.leads())[-1].id
    assert await wait_until(lambda: harness.bot.contains(f"Лид добавлен — ID #{lead_id}")), (
        f"no confirmation for lead #{lead_id} in {harness.bot.texts()}"
    )


async def _save_distinct_leads(harness, count: int) -> None:
    for index in range(count):
        harness.extraction._results = [distinct_result(index)]
        await harness.send_text(f"ТОО Ромашка {NAMES[index]}")
        await harness.send_command("/done")
        await harness.tap(CB_ADD)
        lead_id = (await harness.leads())[-1].id
        assert lead_id == index + 1, "the leads were not saved independently"
        assert await wait_until(
            lambda lead_id=lead_id: harness.bot.contains(
                f"Лид добавлен — ID #{lead_id}, строка {harness.sheets.row}"
            )
        )


# ---------------- FIX-24 ----------------
async def test_adding_a_lead_sends_exactly_one_message(harness):
    await _save_one_lead(harness)

    confirmations = [t for t in harness.bot.texts() if "Лид добавлен" in t]
    assert len(confirmations) == 1, f"expected one confirmation, got {confirmations}"
    assert f"Лид добавлен — ID #1, строка {harness.sheets.row}" in confirmations[0]
    # The old second message («📍 Строка N в таблице готова») must be gone.
    assert not any("в таблице готова" in t for t in harness.bot.texts())
    assert not any("Строка #" in t for t in harness.bot.texts())


async def test_the_single_confirmation_is_sent_after_the_row_exists(harness):
    """The row number in the message is the one that was really written."""
    await _save_one_lead(harness)

    lead = (await harness.leads())[0]
    assert lead.sheet_row == harness.sheets.row
    assert len(harness.sheets.appends) == 1
    assert f"строка {lead.sheet_row}" in harness.bot.last_message().text


async def test_the_confirmation_carries_the_link_when_the_sheet_is_configured(
    harness, monkeypatch
):
    url = "https://docs.google.com/spreadsheets/d/abc123/edit"
    monkeypatch.setattr(harness.container.settings, "SHEET_PUBLIC_URL", url)

    await _save_one_lead(harness)

    text = harness.bot.last_message().text
    assert f"Лид добавлен — ID #1, строка {harness.sheets.row}" in text
    assert f'<a href="{url}">открыть таблицу</a>' in text


async def test_the_deferred_notice_is_also_a_single_message(harness):
    harness.sheets.row = None  # the Sheets write fails

    await _save_one_lead(harness)

    confirmations = [t for t in harness.bot.texts() if "Лид добавлен" in t]
    assert len(confirmations) == 1
    assert "Синхронизация с таблицей чуть задержится" in confirmations[0]
    assert "/resync" in confirmations[0]
    assert (await harness.leads())[0].sheet_row is None


async def test_a_duplicate_chosen_as_new_also_sends_one_message(harness):
    await _save_one_lead(harness)

    # Same name + city, no shared hard identifier → the bot asks, the user says "new".
    harness.extraction._results = [ExtractionResult(company_name="Ромашка", city="Алматы")]
    await harness.send_text("ещё раз Ромашка")
    await harness.send_command("/done")
    await harness.tap(CB_DUP_NEW)

    assert await wait_until(lambda: harness.bot.contains("ID #2"))
    confirmations = [t for t in harness.bot.texts() if "ID #2" in t]
    assert len(confirmations) == 1, f"expected one confirmation, got {confirmations}"
    assert "Лид добавлен — ID #2" in confirmations[0]
    assert "строка" in confirmations[0]


async def test_a_merge_still_reports_the_row_separately(harness):
    """The merge path keeps its own wording: the caller already confirmed the merge."""
    await _save_one_lead(harness)

    harness.extraction._results = [full_result()]  # same phone → strong match
    await harness.send_text("ТОО Ромашка, Алматы, +7 700 123 45 67")
    await harness.send_command("/done")

    assert await wait_until(lambda: harness.bot.contains("обновлён лид #1"))
    assert await wait_until(lambda: harness.bot.contains(f"Строка #{harness.sheets.row}"))


# ---------------- FIX-25b: /search ----------------
@pytest.mark.asyncio
async def test_search_is_limited_and_newest_first(session_factory):
    svc = LeadService(session_factory)
    ids = [
        (await svc.add_lead(1, ExtractionResult(company_name=f"Ромашка {index}"))).id
        for index in range(1, 26)
    ]
    await svc.add_lead(1, ExtractionResult(company_name="Другая компания"))

    found = await svc.search_leads(1, "Ромашка", limit=20)

    assert len(found) == 20, "the result set must be bounded"
    assert [lead.id for lead in found] == list(reversed(ids[5:])), "not newest-first"
    assert found[0].id == ids[-1]


@pytest.mark.asyncio
async def test_search_honours_a_custom_limit(session_factory):
    svc = LeadService(session_factory)
    for index in range(5):
        await svc.add_lead(1, ExtractionResult(company_name=f"Ромашка {index}"))

    assert len(await svc.search_leads(1, "Ромашка", limit=2)) == 2
    assert len(await svc.search_leads(1, "Ромашка", limit=4)) == 4
    assert await svc.search_leads(1, "Ромашка", limit=0) == []


async def test_the_search_command_uses_the_configured_limit(harness, monkeypatch):
    monkeypatch.setattr(harness.container.settings, "SEARCH_RESULT_LIMIT", 3)
    await _save_distinct_leads(harness, 5)

    await harness.send_command("/search Ромашка")

    text = harness.bot.last_message().text
    listed = [line for line in text.split("\n") if line.strip().startswith("#")]
    assert len(listed) == 3, f"the limit was not honoured: {text!r}"
    assert listed[0].startswith("#5") and listed[1].startswith("#4")
    assert not any(item.startswith("#2") for item in listed), "not newest-first"


# ---------------- FIX-25c: /last ----------------
async def test_the_last_command_uses_the_configured_limit(harness, monkeypatch):
    monkeypatch.setattr(harness.container.settings, "LAST_LEADS_LIMIT", 2)
    await _save_distinct_leads(harness, 4)

    await harness.send_command("/last")

    text = harness.bot.last_message().text
    listed = [line for line in text.split("\n") if line.strip().startswith("#")]
    assert len(listed) == 2, f"the limit was not honoured: {text!r}"
    assert listed[0].startswith("#4") and listed[1].startswith("#3"), "not newest-first"


async def test_the_last_command_defaults_to_five(harness):
    await _save_distinct_leads(harness, 7)

    await harness.send_command("/last")

    text = harness.bot.last_message().text
    listed = [line for line in text.split("\n") if line.strip().startswith("#")]
    assert len(listed) == DEFAULT_LAST_LEADS_LIMIT == 5


def test_the_documented_defaults_match_the_configuration():
    from app.config import Settings

    assert Settings.model_fields["LAST_LEADS_LIMIT"].default == DEFAULT_LAST_LEADS_LIMIT == 5
    assert (
        Settings.model_fields["SEARCH_RESULT_LIMIT"].default
        == DEFAULT_SEARCH_RESULT_LIMIT
        == 20
    )
