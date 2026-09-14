"""Inline keyboards for the bot UX.

Buttons never carry a plain emoji in ``text``: the icon is a premium (custom)
emoji supplied through ``icon_custom_emoji_id`` (ids live in ``app.bot.premium``).
"""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.premium import emoji_id

# Callback data constants.
CB_DONE = "done"
CB_CANCEL = "cancel"
CB_ADD = "add"
CB_EDIT = "edit"
CB_EDIT_DONE = "edit_done"
CB_FIELD_PREFIX = "field:"
CB_DUP_SAME = "dup_same"
CB_DUP_NEW = "dup_new"
CB_DUP_CONTACT = "dup_contact"

# Editable fields shown by "Исправить".
EDIT_FIELDS: list[tuple[str, str]] = [
    ("company_name", "Название"),
    ("category", "Категория"),
    ("city", "Город"),
    ("address", "Адрес"),
    ("phone_raw", "Телефон"),
    ("whatsapp_number", "WhatsApp"),
    ("email", "Email"),
    ("instagram", "Instagram"),
    ("telegram", "Telegram"),
    ("website", "Сайт"),
    ("description", "Описание"),
    ("contact_person", "Контакт"),
    ("services", "Услуги"),
    ("tags", "Теги"),
    ("source_guess", "Источник"),
]

# Field key → premium emoji name for the field buttons.
FIELD_EMOJI: dict[str, str] = {
    "company_name": "company",
    "category": "category",
    "city": "city",
    "address": "address",
    "phone_raw": "phone",
    "whatsapp_number": "whatsapp",
    "email": "email",
    "instagram": "instagram",
    "telegram": "telegram",
    "website": "website",
    "description": "description",
    "contact_person": "contact",
    "services": "services",
    "tags": "tags",
    "source_guess": "source",
}


def _field_button(key: str, label: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=label,
        callback_data=f"{CB_FIELD_PREFIX}{key}",
        icon_custom_emoji_id=emoji_id(FIELD_EMOJI[key]),
    )


def collecting_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Готово", callback_data=CB_DONE, icon_custom_emoji_id=emoji_id("check")
                ),
                InlineKeyboardButton(
                    text="Отмена",
                    callback_data=CB_CANCEL,
                    icon_custom_emoji_id=emoji_id("cross"),
                ),
            ]
        ]
    )


def review_keyboard(has_minimum: bool = True) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    if has_minimum:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="Добавить", callback_data=CB_ADD, icon_custom_emoji_id=emoji_id("check")
                )
            ]
        )
    buttons.append(
        [
            InlineKeyboardButton(
                text="Исправить", callback_data=CB_EDIT, icon_custom_emoji_id=emoji_id("pencil")
            ),
            InlineKeyboardButton(
                text="Отмена", callback_data=CB_CANCEL, icon_custom_emoji_id=emoji_id("cross")
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def fields_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(EDIT_FIELDS), 2):
        chunk = EDIT_FIELDS[i : i + 2]
        rows.append([_field_button(key, label) for key, label in chunk])
    rows.append(
        [
            InlineKeyboardButton(
                text="Готово", callback_data=CB_EDIT_DONE, icon_custom_emoji_id=emoji_id("check")
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def duplicate_keyboard() -> InlineKeyboardMarkup:
    # No emoji in the original texts and no matching premium id in the owner's
    # map → texts stay plain (and untouched) here.
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Это тот же лид", callback_data=CB_DUP_SAME)],
            [InlineKeyboardButton(text="Это новый лид", callback_data=CB_DUP_NEW)],
            [
                InlineKeyboardButton(
                    text="Объединить, но обновить контакт", callback_data=CB_DUP_CONTACT
                )
            ],
        ]
    )
