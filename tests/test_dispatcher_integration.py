"""Integration tests: synthetic Telegram updates through the real dispatcher.

These tests exist because the previous suite tested services in isolation and
therefore missed a production 500: ``cmd_start`` awaited a *synchronous*
``SessionBufferService.cancel``, so ``/start`` raised
``TypeError: object NoneType can't be used in 'await' expression``.

Everything here goes through ``Dispatcher.feed_update`` with updates parsed from
raw Telegram JSON, exactly like ``app.main.webhook``. No test opens a network
connection (see the ``no_network`` guard).

Premium emoji are covered here as well: every message carrying a
``<tg-emoji>`` tag must be sent with ``parse_mode=HTML``, and every button must
carry a custom-emoji icon with a clean, emoji-free label.
"""
from __future__ import annotations

from aiogram.enums import ParseMode

from app.bot.keyboards import CB_ADD, CB_CANCEL, CB_DONE, CB_DUP_NEW, CB_EDIT, CB_EDIT_DONE
from app.bot.premium import EMOJI_IDS
from app.bot.states import LeadForm
from app.schemas.extraction import ExtractionResult
from app.services.extraction import ExtractionError
from tests.integration_harness import (
    OWNER_USER_ID,
    STRANGER_USER_ID,
    assert_valid_telegram_html,
    has_plain_emoji,
    harness,  # noqa: F401  — imported fixture
    iter_buttons,
    no_network,  # noqa: F401  — autouse imported fixture (blocks non-loopback sockets)
    wait_until,
)


def full_result() -> ExtractionResult:
    return ExtractionResult(
        company_name="Ромашка",
        city="Алматы",
        phone_raw="+7 700 123 45 67",
        phone_e164="+77001234567",
        instagram="romashka",
        source_guess="2gis",
    )


def callback_data(reply_markup) -> set[str]:
    if reply_markup is None:
        return set()
    return {
        button.callback_data
        for row in reply_markup.inline_keyboard
        for button in row
        if button.callback_data
    }


# ---------------- regression: the production 500 ----------------
async def test_start_cancels_pending_buffer_and_greets(harness):
    """/start must not await the synchronous buffer cancel (prod 500).

    The session is put into Collecting with a live finalize timer first, so the
    handler walks the exact code path that crashed.
    """
    await harness.send_text("ТОО Ромашка, Алматы, +7 700 123 45 67")
    assert harness.container.session_buffer.has_pending(OWNER_USER_ID) is True

    await harness.send_command("/start")  # must not raise

    assert harness.bot.contains("LeadForge AI")
    assert harness.container.session_buffer.has_pending(OWNER_USER_ID) is False
    assert await harness.fsm_state() is None

    greeting = harness.bot.last_message()
    assert greeting is not None and "<tg-emoji emoji-id=" in greeting.text
    assert greeting.parse_mode == ParseMode.HTML


# ---------------- every documented command ----------------
async def test_every_documented_command_is_handled_and_replies(harness):
    harness.extraction._results = [full_result()]

    async def run(command_text: str) -> str:
        before = len(harness.bot.messages())
        await harness.send_command(command_text)
        sent = harness.bot.messages()
        assert len(sent) > before, f"{command_text} produced no outgoing message"
        return sent[-1].text

    assert "LeadForge AI" in await run("/start")
    help_text = await run("/help")
    assert "/new" in help_text and "/done" in help_text
    assert "Собираю лид" in await run("/new")

    await harness.send_text("ТОО Ромашка, Алматы, +7 700 123 45 67")
    assert "Добавить в таблицу" in await run("/done")

    assert "Отменено" in await run("/cancel")
    assert "Пока нет добавленных лидов" in await run("/last")
    assert "Использование" in await run("/search")
    assert "Ничего не найдено" in await run("/search Ромашка")
    assert "Статистика" in await run("/stats")
    assert "Нечего откатывать" in await run("/undo")
    assert "Настройки" in await run("/settings")


# ---------------- scenario (а): garbage input ----------------
async def test_garbage_text_creates_no_lead_and_asks_for_details(harness):
    harness.extraction._results = [ExtractionResult()]  # nothing recognised

    await harness.send_text("привет как дела вообще")
    await harness.send_command("/done")

    assert await harness.lead_count() == 0
    assert harness.bot.contains("Не удалось распознать компанию")
    assert await harness.fsm_state() is None
    sessions = await harness.sessions()
    assert len(sessions) == 1
    assert sessions[0].status == "cancelled"
    assert sessions[0].resulting_lead_id is None


# ---------------- scenario (б): no name and no contact ----------------
async def test_review_without_name_or_contact_hides_add_and_refuses_callback(harness):
    harness.extraction._results = [ExtractionResult(city="Алматы")]

    await harness.send_text("Алматы")
    await harness.send_command("/done")

    assert await harness.fsm_state() == LeadForm.Reviewing.state
    markup = harness.bot.last_reply_markup()
    assert markup is not None
    assert CB_ADD not in callback_data(markup), "«Добавить» must be unavailable"

    # A crafted callback must still be refused by the flow.
    await harness.tap(CB_ADD)

    assert await harness.lead_count() == 0
    assert harness.bot.contains("Заполните название")


# ---------------- scenario (в): user outside the allowlist ----------------
async def test_user_outside_allowlist_is_blocked_neutrally(harness):
    await harness.send_command("/start", user_id=STRANGER_USER_ID)
    await harness.send_text(
        "ТОО Ромашка, Алматы, +7 700 123 45 67", user_id=STRANGER_USER_ID
    )
    await harness.tap(CB_ADD, user_id=STRANGER_USER_ID)

    assert harness.bot.contains("Это приватный бот.")
    assert not harness.bot.contains("LeadForge AI")
    assert harness.extraction.calls == [], "extraction must never run for blocked users"
    assert await harness.sessions() == []
    assert await harness.lead_count() == 0
    assert await harness.fsm_state(user_id=STRANGER_USER_ID) is None
    # Blocked callback queries are acknowledged so the client spinner stops.
    assert any(c.method == "answer_callback_query" for c in harness.bot.calls)


# ---------------- scenario (г): full happy path ----------------
async def test_happy_path_add_lead_and_sync_to_sheets(harness):
    harness.extraction._results = [full_result()]

    await harness.send_text("ТОО Ромашка, Алматы, +7 700 123 45 67")
    await harness.send_command("/done")

    markup = harness.bot.last_reply_markup()
    assert CB_ADD in callback_data(markup)
    assert await harness.fsm_state() == LeadForm.Reviewing.state

    await harness.tap(CB_ADD)

    leads = await harness.leads()
    assert len(leads) == 1
    lead = leads[0]
    assert lead.owner_user_id == OWNER_USER_ID
    assert lead.company_name == "Ромашка"
    assert lead.phone == "+77001234567"
    assert lead.instagram == "romashka"
    assert harness.bot.contains(f"Лид добавлен — ID #{lead.id}")

    # Sheet sync runs as a background task; wait for it instead of racing.
    assert await wait_until(lambda: len(harness.sheets.synced) == 1)
    assert harness.sheets.synced[0].id == lead.id
    assert await wait_until(lambda: harness.bot.contains(f"Строка #{harness.sheets.row}"))

    refreshed = (await harness.leads())[0]
    assert refreshed.sheet_row == harness.sheets.row


# ---------------- scenario (д): /done beats the buffer timeout ----------------
async def test_done_finalizes_before_buffer_timeout(harness):
    assert harness.container.session_buffer.timeout_seconds >= 60, "timer must be slow"
    harness.extraction._results = [full_result()]

    await harness.send_text("ТОО Ромашка, Алматы, +7 700 123 45 67")
    assert harness.container.session_buffer.has_pending(OWNER_USER_ID) is True

    await harness.send_command("/done")

    assert await harness.fsm_state() == LeadForm.Reviewing.state
    assert harness.container.session_buffer.has_pending(OWNER_USER_ID) is False
    assert len(harness.extraction.calls) == 1


# ---------------- scenario (е): /cancel ----------------
async def test_cancel_clears_session_and_marks_it_cancelled(harness):
    await harness.send_text("ТОО Ромашка, Алматы")
    assert await harness.fsm_state() == LeadForm.Collecting.state
    sessions = await harness.sessions()
    assert len(sessions) == 1 and sessions[0].status == "collecting"

    await harness.send_command("/cancel")

    assert await harness.fsm_state() is None
    assert harness.container.session_buffer.has_pending(OWNER_USER_ID) is False
    assert harness.bot.contains("Отменено")
    sessions = await harness.sessions()
    assert sessions[0].status == "cancelled"
    assert await harness.lead_count() == 0


# ---------------- manual fallback (regression: cancel used user_id=0) ----------------
async def test_manual_entry_without_identifier_cancels_real_user_timer(harness):
    harness.extraction.error = ExtractionError("LLM недоступен")

    await harness.send_text("непонятный текст")
    await harness.send_command("/done")
    assert await harness.fsm_state() == LeadForm.ManualEntry.state
    assert harness.bot.contains("Введите название компании")

    async def _noop() -> None:
        return None

    # 4 of the 5 manual steps; then plant a live timer for the *real* user id.
    for _ in range(4):
        await harness.send_text("-")
    harness.container.session_buffer.schedule(OWNER_USER_ID, _noop)
    assert harness.container.session_buffer.has_pending(OWNER_USER_ID) is True

    await harness.send_text("-")  # last step, nothing usable → cancel

    assert await harness.lead_count() == 0
    assert harness.bot.contains("Не удалось распознать компанию")
    assert await harness.fsm_state() is None
    assert harness.container.session_buffer.has_pending(OWNER_USER_ID) is False


async def test_manual_entry_happy_path_reaches_review_and_saves(harness):
    """Completing manual entry used to raise ValidationError (source_guess="manual")."""
    harness.extraction.error = ExtractionError("LLM недоступен")

    await harness.send_text("непонятный текст")
    await harness.send_command("/done")
    assert await harness.fsm_state() == LeadForm.ManualEntry.state

    for answer in ("Ромашка", "+7 700 123 45 67", "Алматы", "-", "-"):
        await harness.send_text(answer)

    assert await harness.fsm_state() == LeadForm.Reviewing.state
    markup = harness.bot.last_reply_markup()
    assert CB_ADD in callback_data(markup)

    await harness.tap(CB_ADD)

    leads = await harness.leads()
    assert len(leads) == 1
    assert leads[0].company_name == "Ромашка"
    assert leads[0].phone == "+77001234567"
    assert leads[0].city == "Алматы"
    assert leads[0].source == "manual"


# ---------------- extra: resync and duplicate confirmation ----------------
async def test_resync_pushes_unsynced_leads(harness):
    harness.extraction._results = [full_result()]
    harness.sheets.row = None  # first automatic sync fails (lead stays unsynced)

    await harness.send_text("ТОО Ромашка, Алматы, +7 700 123 45 67")
    await harness.send_command("/done")
    await harness.tap(CB_ADD)

    assert await wait_until(lambda: len(harness.sheets.synced) == 1)
    assert (await harness.leads())[0].sheet_row is None
    assert harness.bot.contains("синхронизация с таблицей чуть задержится")

    harness.sheets.row = 42
    await harness.send_command("/resync")

    assert harness.bot.contains("Синхронизирую 1")
    assert harness.bot.contains("Синхронизировано: 1/1")
    assert (await harness.leads())[0].sheet_row == 42


async def test_medium_duplicate_asks_before_creating(harness):
    harness.extraction._results = [full_result()]
    await harness.send_text("ТОО Ромашка, Алматы, +7 700 123 45 67")
    await harness.send_command("/done")
    await harness.tap(CB_ADD)
    assert await harness.lead_count() == 1

    # Same name + same city, no shared hard identifier → medium match.
    harness.extraction._results = [ExtractionResult(company_name="Ромашка", city="Алматы")]
    await harness.send_text("ещё раз Ромашка")
    await harness.send_command("/done")

    assert await harness.fsm_state() == LeadForm.ConfirmingDuplicate.state
    assert harness.bot.contains("Похоже на уже существующий лид")
    assert await harness.lead_count() == 1, "duplicate must not be saved silently"

    await harness.tap(CB_DUP_NEW)
    assert await harness.lead_count() == 2


# ---------------- premium emoji ----------------
async def test_premium_emoji_messages_are_sent_with_html_parse_mode(harness):
    """A <tg-emoji> tag only renders when the message is HTML — never plain text."""
    harness.extraction._results = [full_result()]

    await harness.send_command("/start")
    await harness.send_text("ТОО Ромашка, Алматы, +7 700 123 45 67")
    await harness.send_command("/done")
    await harness.tap(CB_EDIT)  # fields keyboard
    await harness.tap(CB_EDIT_DONE)  # back to the review card
    await harness.tap(CB_ADD)
    assert await wait_until(lambda: len(harness.sheets.synced) == 1)
    await harness.send_command("/stats")
    await harness.send_command("/settings")
    await harness.send_command("/last")
    await harness.send_command("/resync")

    premium = harness.bot.premium_messages()
    assert len(premium) >= 6, "expected premium emoji in start/review/cancel/sync messages"
    for call in premium:
        assert call.parse_mode == ParseMode.HTML, (
            f"premium emoji sent without parse_mode=HTML: {call.text!r}"
        )
        assert_valid_telegram_html(call.text)


async def test_action_buttons_use_custom_emoji_icon_and_clean_text(harness):
    """Buttons keep their old label but move the icon into icon_custom_emoji_id."""
    harness.extraction._results = [full_result()]

    await harness.send_text("ТОО Ромашка, Алматы, +7 700 123 45 67")
    await harness.send_command("/done")
    await harness.tap(CB_EDIT)

    buttons = {b.callback_data: b for b in harness.bot.all_buttons() if b.callback_data}
    assert buttons[CB_DONE].text == "Готово"
    assert buttons[CB_CANCEL].text == "Отмена"
    assert buttons[CB_ADD].text == "Добавить"
    assert buttons[CB_EDIT].text == "Исправить"

    assert buttons[CB_DONE].icon_custom_emoji_id == EMOJI_IDS["check"]
    assert buttons[CB_CANCEL].icon_custom_emoji_id == EMOJI_IDS["cross"]
    assert buttons[CB_ADD].icon_custom_emoji_id == EMOJI_IDS["check"]
    assert buttons[CB_EDIT].icon_custom_emoji_id == EMOJI_IDS["pencil"]

    for button in harness.bot.all_buttons():
        assert not has_plain_emoji(button.text), f"plain emoji in button {button.text!r}"
        assert button.icon_custom_emoji_id and button.icon_custom_emoji_id.isdigit()

    # The "Исправить" keyboard is fully icon-driven too.
    field_buttons = [b for b in iter_buttons(harness.bot.last_reply_markup())]
    assert field_buttons and all(b.icon_custom_emoji_id for b in field_buttons)


async def test_html_characters_in_lead_data_are_escaped(harness):
    """Escaping regression: a company name with <, & and > must not break the card."""
    hostile_name = 'ООО "<Ромашка>" & Co'
    harness.extraction._results = [
        ExtractionResult(
            company_name=hostile_name,
            city="Алматы",
            description="5 < 7 и 9 > 8, скидка & подарок",
            services=["<b>услуга</b>"],
            uncertain_fields=["<script>city</script>"],
        )
    ]

    await harness.send_text('ТОО "<Ромашка>" & Co, Алматы')
    await harness.send_command("/done")

    card = harness.bot.last_message().text
    assert 'ООО "&lt;Ромашка&gt;" &amp; Co' in card
    assert "<Ромашка>" not in card and "<script>" not in card
    assert_valid_telegram_html(card)

    # Same data through the review → save → /last report path.
    await harness.tap(CB_ADD)
    await harness.send_command("/last")

    report = harness.bot.last_message().text
    assert "&lt;Ромашка&gt;" in report
    assert_valid_telegram_html(report)
    assert (await harness.leads())[0].company_name == hostile_name


async def test_duplicate_prompt_escapes_existing_lead_summary(harness):
    hostile_name = 'ООО "<Ромашка>" & Co'
    harness.extraction._results = [ExtractionResult(company_name=hostile_name, city="Алматы")]
    await harness.send_text('ТОО "<Ромашка>" & Co')
    await harness.send_command("/done")
    await harness.tap(CB_ADD)
    assert await harness.lead_count() == 1

    harness.extraction._results = [ExtractionResult(company_name=hostile_name, city="Алматы")]
    await harness.send_text("снова та же компания")
    await harness.send_command("/done")

    assert await harness.fsm_state() == LeadForm.ConfirmingDuplicate.state
    prompt = harness.bot.last_message().text
    assert "Похоже на уже существующий лид" in prompt
    assert "<Ромашка>" not in prompt
    assert_valid_telegram_html(prompt)
