"""Premium (custom) emoji: ids, tags, escaping and keyboards — no network.

The live Bot API behaviour was verified by hand before this change
(``sendMessage`` with ``parse_mode=HTML`` and ``<tg-emoji emoji-id="…">`` → 200,
inline button with ``icon_custom_emoji_id`` → 200). What these tests lock in is
the shape of what the bot emits and — above all — that no dynamic value ever
reaches an HTML message unescaped, which is the one thing Telegram answers with
400 "can't parse entities".

The ids are pinned twice on purpose: ``OWNER_IDS`` is the replacement map the
owner approved, ``PREMIUM_EMOJI`` is the editable source of truth. If someone
edits an id by accident, this test fails.
"""
from __future__ import annotations

import pytest

from app.bot import cards
from app.bot.keyboards import (
    CB_ADD,
    CB_CANCEL,
    CB_DONE,
    CB_EDIT,
    CB_EDIT_DONE,
    EDIT_FIELDS,
    FIELD_EMOJI,
    collecting_keyboard,
    duplicate_keyboard,
    fields_keyboard,
    review_keyboard,
)
from app.bot.premium import EMOJI_IDS, PREMIUM_EMOJI, emoji, emoji_id
from app.models import Lead
from app.schemas.extraction import ExtractionResult
from tests.integration_harness import (
    TG_EMOJI_RE,
    assert_valid_telegram_html,
    has_plain_emoji,
    iter_buttons,
)

# The owner's replacement map: premium emoji name → id, verbatim.
OWNER_IDS: dict[str, str] = {
    "bot": "6030400221232501136",        # 👋 приветствие → 🤖 Бот
    "stats": "5870921681735781843",      # 📊 статистика
    "settings": "5870982283724328568",   # ⚙️ настройки
    "check": "5870633910337015697",      # ✅ галочка/успех
    "cross": "5870657884844462243",      # ❌ отмена/ошибка
    "pencil": "5870676941614354370",     # ✏️ исправить
    "company": "5873147866364514353",    # 🏢 компания
    "city": "6042011682497106307",       # 📍 город
    "address": "6042011682497106307",    # 🏠 адрес (та же геометка)
    "phone": "6039451237743595514",      # 📞 телефон
    "email": "5870528606328852614",      # ✉️ email
    "instagram": "6035128606563241721",  # 📸 instagram
    "telegram": "6039422865189638057",   # ✈️ telegram
    "website": "5769289093221454192",    # 🌐 сайт
    "category": "5886285355279193209",   # 🏷️ категория
    "services": "5884479287171485878",   # 🔧 услуги
    "rating": "5870930636742595124",     # ⭐ рейтинг
    "reviews": "5870772616305839506",    # кол-во отзывов → 👥 Люди
    "contact": "5870994129244131212",    # 👤 контактное лицо
    "description": "5870753782874246579",  # 📝 описание
    "warning": "6028435952299413210",    # ⚠️ предупреждение → ℹ Инфо
    "analyzing": "5345906554510012647",  # 🔎 анализирую → 🔄 Загрузка
    "collecting": "6039802767931871481",  # 📥 собираю → ⬇ Скачать
    "row": "5870528606328852614",        # 📄 строка в таблице → 📁 Файл
    "time": "5775896410780079073",       # ⏰/🕓 время
    "party": "6041731551845159060",      # 🎉 успех
    "lock": "6037249452824072506",       # 🔒
    "unlock": "6037496202990194718",     # 🔓
}


def _expected_buttons() -> dict[str, str]:
    return {
        CB_DONE: "check",
        CB_CANCEL: "cross",
        CB_ADD: "check",
        CB_EDIT: "pencil",
        CB_EDIT_DONE: "check",
    }


# ---------------- premium emoji table ----------------
@pytest.mark.parametrize("name,expected", sorted(OWNER_IDS.items()))
def test_owner_ids_are_wired_verbatim(name, expected):
    assert emoji_id(name) == expected


def test_every_entry_is_a_usable_tag():
    for name, (emoji_id_, glyph) in PREMIUM_EMOJI.items():
        assert emoji_id_.isdigit(), name
        assert glyph, f"{name} needs a fallback glyph"
        rendered = emoji(name)
        match = TG_EMOJI_RE.fullmatch(rendered)
        assert match, f"{name} rendered as {rendered!r}"
        assert match.group(1) == emoji_id_
        assert match.group(2) == glyph


def test_emoji_renders_the_expected_snippet():
    assert (
        emoji("check")
        == '<tg-emoji emoji-id="5870633910337015697">✅</tg-emoji>'
    )


def test_emoji_ids_view_matches_the_table():
    assert EMOJI_IDS == {name: id_ for name, (id_, _) in PREMIUM_EMOJI.items()}


def test_fallback_can_be_overridden():
    assert emoji("cross", fallback="X") == (
        '<tg-emoji emoji-id="5870657884844462243">X</tg-emoji>'
    )


def test_unknown_name_raises_keyerror():
    with pytest.raises(KeyError):
        emoji("no-such-emoji")


# ---------------- card escaping ----------------
HOSTILE = ExtractionResult(
    company_name='ООО "<Ромашка>" & Co',
    city="Алматы",
    phone_e164="+77001234567",
    address="ул. Ленина 5 <корпус 2>",
    category="a & b",
    description="5 < 7 > 3 & всё хорошо",
    services=["<b>услуга</b>", "кофе & чай"],
    contact_person='Иван "Директор"',
    rating=4.5,
    reviews_count=17,
    uncertain_fields=["<script>alert(1)</script>"],
)


def test_render_card_escapes_every_dynamic_value():
    card = cards.render_card(HOSTILE)

    # quote() escapes &, < and > (a bare " is legal in Telegram HTML text).
    assert 'ООО "&lt;Ромашка&gt;" &amp; Co' in card
    assert "ул. Ленина 5 &lt;корпус 2&gt;" in card
    assert "5 &lt; 7 &gt; 3 &amp; всё хорошо" in card
    assert "&lt;b&gt;услуга&lt;/b&gt;, кофе &amp; чай" in card
    # No raw markup from the data survived.
    assert "<Ромашка>" not in card
    assert "<b>услуга</b>" not in card
    assert "<script>" not in card
    # Premium emoji are present, and nothing is left as a plain emoji.
    assert card.count("<tg-emoji emoji-id=") >= 8
    assert_valid_telegram_html(card)


def test_render_card_html_is_well_formed_and_plain_emoji_free():
    card = cards.render_card(HOSTILE)
    assert_valid_telegram_html(card)
    assert not has_plain_emoji(TG_EMOJI_RE.sub("", card))


def test_render_card_rating_and_reviews_use_premium_emoji():
    card = cards.render_card(HOSTILE)
    assert f"{emoji('rating')} 4.5" in card
    assert f"({emoji('reviews')} 17 отзывов)" in card


def test_render_card_warns_with_info_emoji():
    card = cards.render_card(ExtractionResult())
    assert emoji("warning") in card
    assert "не найдено название" in card


def test_render_lead_summary_escapes_dynamic_values():
    lead = Lead(
        id=3,
        owner_user_id=1,
        company_name='<b>Ромашка</b>',
        city="Алматы & Ко",
        phone="+77001234567",
        instagram="roma<script>",
        website="https://x.kz/?a=1&b=2",
    )

    summary = cards.render_lead_summary(lead)

    assert "#3" in summary
    assert "&lt;b&gt;Ромашка&lt;/b&gt;" in summary
    assert "Алматы &amp; Ко" in summary
    assert "roma&lt;script&gt;" in summary
    assert "a=1&amp;b=2" in summary
    assert_valid_telegram_html(summary)


# ---------------- keyboards ----------------
def _all_buttons() -> list:
    buttons = []
    for markup in (
        collecting_keyboard(),
        review_keyboard(True),
        review_keyboard(False),
        fields_keyboard(),
        duplicate_keyboard(),
    ):
        buttons.extend(iter_buttons(markup))
    return buttons


def test_every_button_text_is_clean():
    for button in _all_buttons():
        assert button.text and button.text.strip(), "button text must not be empty"
        assert not has_plain_emoji(button.text), f"plain emoji in button {button.text!r}"


def test_action_buttons_carry_the_premium_icon():
    icons = {
        button.callback_data: button.icon_custom_emoji_id
        for button in _all_buttons()
        if button.callback_data
    }
    for callback_data, emoji_name in _expected_buttons().items():
        assert icons[callback_data] == emoji_id(emoji_name), callback_data


def test_action_button_labels_are_the_clean_ones():
    labels = {button.text for button in _all_buttons()}
    assert {"Готово", "Отмена", "Добавить", "Исправить"} <= labels


def test_field_buttons_have_icons_for_every_editable_field():
    assert set(FIELD_EMOJI) == {key for key, _ in EDIT_FIELDS}

    field_icons = {
        button.callback_data: button.icon_custom_emoji_id
        for button in iter_buttons(fields_keyboard())
        if button.callback_data and button.callback_data.startswith("field:")
    }
    assert len(field_icons) == len(EDIT_FIELDS)
    for callback_data, icon in field_icons.items():
        assert icon and icon.isdigit(), f"{callback_data} has no premium icon"


def test_collecting_and_review_keyboards_have_no_emoji_in_text():
    for markup in (collecting_keyboard(), review_keyboard(True)):
        for button in iter_buttons(markup):
            assert not has_plain_emoji(button.text)
            assert button.icon_custom_emoji_id
