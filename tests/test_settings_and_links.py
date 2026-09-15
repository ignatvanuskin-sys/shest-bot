"""FIX-15 (/settings + post-add link) and FIX-16 (honest duplicate-notification state).

Both were «text that does not match the configuration»:

* ``/settings`` printed the sheet only as text and hardcoded «Уведомления о дублях:
  включены» whatever the config said;
* after a successful save the bot wrote «Строка N» with no way to open the table.

These go through the real dispatcher, so the HTML that reaches Telegram is checked as
well (a link with an unescaped ``&`` would be a 400, not a cosmetic bug).
"""
from __future__ import annotations

from app.bot.keyboards import CB_ADD
from app.schemas.extraction import ExtractionResult
from tests.integration_harness import (
    assert_valid_telegram_html,
    harness,  # noqa: F401  — imported fixture
    no_network,  # noqa: F401  — autouse imported fixture (blocks non-loopback sockets)
    wait_until,
)

SHEET_URL = "https://docs.google.com/spreadsheets/d/abc123/edit?usp=sharing"


def full_result():
    return ExtractionResult(
        company_name="Ромашка",
        city="Алматы",
        phone_raw="+7 700 123 45 67",
        phone_e164="+77001234567",
        source_guess="2gis",
    )


# ---------------- FIX-15 ----------------
async def test_settings_shows_a_clickable_link_when_sheet_url_is_set(harness, monkeypatch):
    monkeypatch.setattr(harness.container.settings, "SHEET_PUBLIC_URL", SHEET_URL)

    await harness.send_command("/settings")

    text = harness.bot.last_message().text
    assert f'<a href="{SHEET_URL}">открыть таблицу</a>' in text
    assert_valid_telegram_html(text)


async def test_settings_link_escapes_an_ampersand_in_the_url(harness, monkeypatch):
    """The URL is user configuration; an unescaped «&» would break the message."""
    monkeypatch.setattr(
        harness.container.settings,
        "SHEET_PUBLIC_URL",
        "https://docs.google.com/spreadsheets/d/abc?x=1&y=2",
    )

    await harness.send_command("/settings")

    text = harness.bot.last_message().text
    assert "&amp;y=2" in text
    assert_valid_telegram_html(text)


async def test_settings_falls_back_to_the_sheet_id(harness, monkeypatch):
    monkeypatch.setattr(harness.container.settings, "GOOGLE_SHEET_ID", "sheet-id-42")

    await harness.send_command("/settings")

    text = harness.bot.last_message().text
    assert "https://docs.google.com/spreadsheets/d/sheet-id-42" in text


async def test_settings_says_the_table_is_not_configured(harness):
    """No link, no sheet id → the honest sentence, never a made-up URL."""
    await harness.send_command("/settings")

    text = harness.bot.last_message().text
    assert "Таблица: не задана" in text


async def test_added_lead_message_carries_the_sheet_link(harness, monkeypatch):
    monkeypatch.setattr(harness.container.settings, "SHEET_PUBLIC_URL", SHEET_URL)
    harness.extraction._results = [full_result()]

    await harness.send_text("ТОО Ромашка, Алматы, +7 700 123 45 67")
    await harness.send_command("/done")
    await harness.tap(CB_ADD)

    assert await wait_until(lambda: harness.bot.contains(f"Строка #{harness.sheets.row}"))
    ready = [call for call in harness.bot.messages() if "Строка #" in (call.text or "")]
    assert ready, "the sync message was not sent"
    assert f'<a href="{SHEET_URL}">' in ready[-1].text
    assert_valid_telegram_html(ready[-1].text)


async def test_added_lead_message_has_no_link_without_configuration(harness):
    harness.extraction._results = [full_result()]

    await harness.send_text("ТОО Ромашка, Алматы, +7 700 123 45 67")
    await harness.send_command("/done")
    await harness.tap(CB_ADD)

    assert await wait_until(lambda: harness.bot.contains(f"Строка #{harness.sheets.row}"))
    ready = [call for call in harness.bot.messages() if "Строка #" in (call.text or "")]
    assert "<a href=" not in ready[-1].text, "no link may appear when none is configured"


# ---------------- FIX-16 ----------------
async def test_settings_reports_duplicate_notifications_enabled(harness):
    await harness.send_command("/settings")

    assert "Уведомления о дублях: включены" in harness.bot.last_message().text


async def test_settings_reports_duplicate_notifications_disabled(harness, monkeypatch):
    """The line used to be hardcoded «включены» regardless of the configuration."""
    monkeypatch.setattr(harness.container.settings, "DUP_NOTIFICATIONS_ENABLED", False)

    await harness.send_command("/settings")

    text = harness.bot.last_message().text
    assert "Уведомления о дублях: выключены" in text
    assert "включены" not in text
    assert_valid_telegram_html(text)


# ---------------- FIX-11: /stats wording through the real handler ----------------
async def test_stats_reports_counters_tokens_and_an_honest_zero(harness):
    """With no extraction logged yet the line must say so, not show «$0.000000»."""
    await harness.send_command("/stats")

    text = harness.bot.last_message().text
    assert "Обращений к AI: 0" in text
    assert "Токены: 0 вх. / 0 исх." in text
    assert "Расход на AI: нет данных" in text
    assert "$0.000000" not in text
    assert_valid_telegram_html(text)
