"""aiogram handlers — thin wrappers around the flow orchestration layer.

Every user-facing reply goes through :mod:`app.bot.safe`: a failed send
(Forbidden / chat not found / network) is logged and the update stays
successful — Telegram must never see a 500, it would replay the update.
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.types import CallbackQuery, Message
from aiogram.utils.markdown import html_decoration

from app.bot import flow
from app.bot.keyboards import (
    CB_ADD,
    CB_CANCEL,
    CB_DONE,
    CB_DUP_CONTACT,
    CB_DUP_NEW,
    CB_DUP_SAME,
    CB_EDIT,
    CB_EDIT_DONE,
    CB_FIELD_PREFIX,
    EDIT_FIELDS,
    fields_keyboard,
)
from app.bot.premium import emoji
from app.bot.safe import safe_answer_callback, safe_reply
from app.bot.states import LeadForm
from app.schemas.extraction import ExtractionResult

logger = logging.getLogger(__name__)

router = Router(name="leadforge")

FIELD_LABELS = dict(EDIT_FIELDS)
# Labels for schema fields the keyboard does not offer but callback data can still
# name (the audit reproduced the edit bug with «Рейтинг» = «нет данных»).
FIELD_LABELS.update({"rating": "Рейтинг", "reviews_count": "Отзывы", "has_whatsapp": "WhatsApp"})


def _uid(message_or_callback) -> int:
    return message_or_callback.from_user.id


def _chat(message_or_callback) -> int:
    if isinstance(message_or_callback, Message):
        return message_or_callback.chat.id
    # callback.message can be None (inaccessible message) — fall back to the DM.
    if message_or_callback.message is not None:
        return message_or_callback.message.chat.id
    return message_or_callback.from_user.id


async def _answer_via_callback(callback: CallbackQuery, text: str, *, action: str, **kwargs):
    """Reply in the chat of a callback's message (which may be inaccessible)."""
    message = callback.message
    if message is None:
        return False
    return await safe_reply(message, text, action=action, **kwargs)


# ---------------- commands ----------------
@router.message(CommandStart())
async def cmd_start(message: Message, state, container) -> None:
    container.session_buffer.cancel(_uid(message))
    await state.clear()
    await safe_reply(
        message,
        f"{emoji('bot')} LeadForge AI — собираю лиды в Google Sheets.\n\n"
        "Просто пришлите текст о компании (из 2GIS / Instagram / сайта) одним или "
        "несколькими сообщениями подряд — бот сам распознает данные и покажет карточку.\n\n"
        "Команды: /help",
        action="start_greeting",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await safe_reply(
        message,
        "Команды:\n"
        "/new — начать новый лид\n"
        "/done — закончить сбор буфера сейчас\n"
        "/cancel — отменить текущий сбор\n"
        "/last — последние добавленные лиды\n"
        "/search <запрос> — поиск по базе\n"
        "/stats — статистика и расход на AI\n"
        "/undo — откатить последнее действие\n"
        "/settings — настройки\n"
        "/resync — досинхронизировать лиды в таблицу",
        action="help",
    )


@router.message(Command("new"))
async def cmd_new(message: Message, state, container) -> None:
    user_id = _uid(message)
    container.session_buffer.cancel(user_id)
    await state.clear()
    await flow.start_collection(container, user_id, _chat(message), state, "")


@router.message(Command("done"))
async def cmd_done(message: Message, state, container) -> None:
    await flow.finalize_collection(container, _uid(message), _chat(message), state)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state, container) -> None:
    await flow.cancel_collection(container, _uid(message), state, chat_id=_chat(message))


@router.message(Command("last"))
async def cmd_last(message: Message, container) -> None:
    leads = await container.leads.get_last_leads(_uid(message), limit=5)
    if not leads:
        await safe_reply(message, "Пока нет добавленных лидов.", action="last_empty")
        return
    lines = [_lead_short(lead) for lead in leads]
    await safe_reply(
        message,
        "Последние лиды:\n\n" + "\n\n".join(lines),
        action="last_leads",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("search"))
async def cmd_search(message: Message, container, command: CommandObject) -> None:
    query = (command.args or "").strip()
    if not query:
        await safe_reply(
            message,
            "Использование: /search &lt;название/телефон/instagram/сайт&gt;",
            action="search_usage",
            parse_mode=ParseMode.HTML,
        )
        return
    leads = await container.leads.search_leads(_uid(message), query)
    if not leads:
        await safe_reply(message, "Ничего не найдено.", action="search_empty")
        return
    lines = [_lead_short(lead) for lead in leads]
    await safe_reply(
        message,
        "Найдено:\n\n" + "\n\n".join(lines),
        action="search_results",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message, container) -> None:
    stats = await container.leads.get_stats(_uid(message))
    await safe_reply(
        message,
        f"{emoji('stats')} Статистика:\n"
        f"• Лидов всего: {stats['total']}\n"
        f"• За неделю: {stats['week']}\n"
        f"• Объединено дублей: {stats['duplicates']}\n"
        f"• Расход на AI: ${stats['cost_usd']:.6f}",
        action="stats",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("undo"))
async def cmd_undo(message: Message, container) -> None:
    result = await container.leads.undo_last(_uid(message), sheets=container.sheets)
    await safe_reply(message, result or "Нечего откатывать.", action="undo")


@router.message(Command("settings"))
async def cmd_settings(message: Message, container) -> None:
    settings = container.settings
    sheet_link = (
        f"https://docs.google.com/spreadsheets/d/{settings.GOOGLE_SHEET_ID}"
        if settings.GOOGLE_SHEET_ID
        else "не задана"
    )
    await safe_reply(
        message,
        f"{emoji('settings')} Настройки:\n"
        f"• Город по умолчанию: {html_decoration.quote(str(settings.DEFAULT_CITY))}\n"
        f"• Уведомления о дублях: включены\n"
        f"• Таблица: {html_decoration.quote(sheet_link)}\n\n"
        "Значения задаются через переменные окружения (.env).",
        action="settings",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("resync"))
async def cmd_resync(message: Message, container) -> None:
    leads = await container.leads.get_unsynced_leads(_uid(message))
    if not leads:
        await safe_reply(message, "Нет лидов, ожидающих синхронизации.", action="resync_empty")
        return
    await safe_reply(
        message,
        f"{emoji('collecting')} Синхронизирую {len(leads)} лид(ов)…",
        action="resync_start",
        parse_mode=ParseMode.HTML,
    )
    done = 0
    for lead in leads:
        row = await container.sheets.sync_lead(lead)
        if row:
            await container.leads.update_lead(lead.id, sheet_row=row)
            done += 1
    await safe_reply(
        message,
        f"{emoji('check')} Синхронизировано: {done}/{len(leads)}.",
        action="resync_done",
        parse_mode=ParseMode.HTML,
    )


# ---------------- collection (buffer) ----------------
@router.message(LeadForm.Collecting, F.text)
async def on_collecting_text(message: Message, state, container) -> None:
    await flow.append_message(container, _uid(message), _chat(message), state, message.text)


# While the LLM is working (15–40 s on free models) the dialog is locked: a second
# extraction for the same text produced a second card, and «Добавить» under the
# first card saved the other lead. Such a message is answered, never extracted.
@router.message(LeadForm.Processing, F.text)
async def on_processing_text(message: Message) -> None:
    await safe_reply(
        message,
        f"{emoji('collecting')} Уже обрабатываю предыдущее сообщение — подождите пару секунд.",
        action="already_processing",
        parse_mode=ParseMode.HTML,
    )


@router.message(LeadForm.Reviewing, F.text)
async def on_review_text(message: Message) -> None:
    await safe_reply(
        message,
        f"Используйте кнопки под карточкой: {emoji('check')} Добавить / "
        f"{emoji('pencil')} Исправить / {emoji('cross')} Отмена.",
        action="review_hint",
        parse_mode=ParseMode.HTML,
    )


@router.message(LeadForm.ConfirmingDuplicate, F.text)
async def on_duplicate_text(message: Message) -> None:
    await safe_reply(
        message, "Пожалуйста, выберите один из вариантов под сообщением.", action="duplicate_hint"
    )


@router.message(LeadForm.EditingField, F.text)
async def on_edit_text(message: Message, state, container) -> None:
    data = await state.get_data()
    field = data.get("editing_field")
    if not field:
        await safe_reply(message, "Сначала выберите поле для исправления.", action="edit_no_field")
        return
    extracted = dict(data.get("extracted") or {})
    try:
        updated = flow.apply_edit(extracted, field, message.text)
    except flow.EditValidationError as exc:
        # Nothing is lost: the field stays selected, the old value is untouched and
        # the user is told what the schema expects (previously: ValidationError,
        # no reply at all, dialog stuck in EditingField).
        label = html_decoration.quote(str(FIELD_LABELS.get(field, field)))
        await safe_reply(
            message,
            f"{emoji('warning')} Для «{label}» {exc.hint}.",
            action="edit_invalid_value",
            reply_markup=fields_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return
    session_id = data.get("session_id")
    await state.update_data(extracted=updated, editing_field=None)
    await flow.show_review(
        container, _chat(message), state, ExtractionResult.model_validate(updated), session_id
    )


@router.message(LeadForm.ManualEntry, F.text)
async def on_manual_text(message: Message, state, container) -> None:
    await flow.process_manual_message(
        container, _uid(message), _chat(message), state, message.text
    )


# Idle: any text starts a new collection.
@router.message(StateFilter(None), F.text)
async def on_idle_text(message: Message, state, container) -> None:
    await flow.start_collection(container, _uid(message), _chat(message), state, message.text)


# ---------------- callbacks ----------------
@router.callback_query(F.data == CB_DONE)
async def cb_done(callback: CallbackQuery, state, container) -> None:
    await safe_answer_callback(callback, action="ack_done")
    await flow.finalize_collection(container, _uid(callback), _chat(callback), state)


@router.callback_query(F.data == CB_CANCEL)
async def cb_cancel(callback: CallbackQuery, state, container) -> None:
    await safe_answer_callback(callback, action="ack_cancel")
    await flow.cancel_collection(container, _uid(callback), state, chat_id=_chat(callback))


@router.callback_query(F.data == CB_ADD)
async def cb_add(callback: CallbackQuery, state, container) -> None:
    await safe_answer_callback(callback, action="ack_add")
    await flow.confirm_add(container, _uid(callback), _chat(callback), state)


@router.callback_query(F.data == CB_EDIT)
async def cb_edit(callback: CallbackQuery, state) -> None:
    await safe_answer_callback(callback, action="ack_edit")
    await state.set_state(LeadForm.EditingField)
    await state.update_data(editing_field=None)
    await _answer_via_callback(
        callback,
        f"{emoji('pencil')} Какое поле исправить?",
        action="edit_fields_keyboard",
        reply_markup=fields_keyboard(),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data == CB_EDIT_DONE)
async def cb_edit_done(callback: CallbackQuery, state, container) -> None:
    await safe_answer_callback(callback, action="ack_edit_done")
    data = await state.get_data()
    extracted = ExtractionResult.model_validate(data.get("extracted") or {})
    session_id = data.get("session_id")
    await flow.show_review(container, _chat(callback), state, extracted, session_id)


@router.callback_query(F.data.startswith(CB_FIELD_PREFIX))
async def cb_select_field(callback: CallbackQuery, state) -> None:
    await safe_answer_callback(callback, action="ack_select_field")
    field = callback.data[len(CB_FIELD_PREFIX):]
    await state.update_data(editing_field=field)
    label = FIELD_LABELS.get(field, field)
    await _answer_via_callback(
        callback,
        f"{emoji('pencil')} Введите новое значение для "
        f"«{html_decoration.quote(str(label))}»:",
        action="edit_field_prompt",
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data == CB_DUP_SAME)
async def cb_dup_same(callback: CallbackQuery, state, container) -> None:
    await safe_answer_callback(callback, action="ack_dup_same")
    await flow.handle_duplicate_choice(container, _uid(callback), _chat(callback), state, "same")


@router.callback_query(F.data == CB_DUP_NEW)
async def cb_dup_new(callback: CallbackQuery, state, container) -> None:
    await safe_answer_callback(callback, action="ack_dup_new")
    await flow.handle_duplicate_choice(container, _uid(callback), _chat(callback), state, "new")


@router.callback_query(F.data == CB_DUP_CONTACT)
async def cb_dup_contact(callback: CallbackQuery, state, container) -> None:
    await safe_answer_callback(callback, action="ack_dup_contact")
    await flow.handle_duplicate_choice(
        container, _uid(callback), _chat(callback), state, "contact"
    )


def _lead_short(lead) -> str:
    """One-line lead summary; HTML (escaped) because the caller sends it as HTML."""
    parts = [f"#{lead.id}"]
    if lead.company_name:
        parts.append(f"{emoji('company')} {html_decoration.quote(str(lead.company_name))}")
    if lead.city:
        parts.append(f"{emoji('city')} {html_decoration.quote(str(lead.city))}")
    if lead.phone:
        parts.append(f"{emoji('phone')} {html_decoration.quote(str(lead.phone))}")
    if lead.instagram:
        parts.append(f"{emoji('instagram')} @{html_decoration.quote(str(lead.instagram))}")
    if lead.sheet_row:
        parts.append(f"{emoji('row')} строка {html_decoration.quote(str(lead.sheet_row))}")
    return " — ".join(parts)
