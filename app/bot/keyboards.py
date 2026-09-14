"""Inline keyboards for the bot UX."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

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


def collecting_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Готово", callback_data=CB_DONE),
                InlineKeyboardButton(text="❌ Отмена", callback_data=CB_CANCEL),
            ]
        ]
    )


def review_keyboard(has_minimum: bool = True) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    if has_minimum:
        buttons.append([InlineKeyboardButton(text="✅ Добавить", callback_data=CB_ADD)])
    buttons.append(
        [
            InlineKeyboardButton(text="✏️ Исправить", callback_data=CB_EDIT),
            InlineKeyboardButton(text="❌ Отмена", callback_data=CB_CANCEL),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def fields_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(EDIT_FIELDS), 2):
        chunk = EDIT_FIELDS[i : i + 2]
        rows.append(
            [
                InlineKeyboardButton(text=label, callback_data=f"{CB_FIELD_PREFIX}{key}")
                for key, label in chunk
            ]
        )
    rows.append([InlineKeyboardButton(text="✅ Готово", callback_data=CB_EDIT_DONE)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def duplicate_keyboard() -> InlineKeyboardMarkup:
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
