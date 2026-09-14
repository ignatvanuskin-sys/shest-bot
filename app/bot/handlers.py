"""aiogram handlers — thin wrappers around the flow orchestration layer."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.types import CallbackQuery, Message

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
from app.bot.states import LeadForm
from app.schemas.extraction import ExtractionResult

logger = logging.getLogger(__name__)

router = Router(name="leadforge")

FIELD_LABELS = dict(EDIT_FIELDS)


def _uid(message_or_callback) -> int:
    return message_or_callback.from_user.id


def _chat(message_or_callback) -> int:
    return message_or_callback.chat.id if isinstance(message_or_callback, Message) \
        else message_or_callback.message.chat.id


# ---------------- commands ----------------
@router.message(CommandStart())
async def cmd_start(message: Message, state, container) -> None:
    await container.session_buffer.cancel(_uid(message))
    await state.clear()
    await message.answer(
        "👋 LeadForge AI — собираю лиды в Google Sheets.\n\n"
        "Просто пришлите текст о компании (из 2GIS / Instagram / сайта) одним или "
        "несколькими сообщениями подряд — бот сам распознает данные и покажет карточку.\n\n"
        "Команды: /help"
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "Команды:\n"
        "/new — начать новый лид\n"
        "/done — закончить сбор буфера сейчас\n"
        "/cancel — отменить текущий сбор\n"
        "/last — последние добавленные лиды\n"
        "/search <запрос> — поиск по базе\n"
        "/stats — статистика и расход на AI\n"
        "/undo — откатить последнее действие\n"
        "/settings — настройки\n"
        "/resync — досинхронизировать лиды в таблицу"
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
        await message.answer("Пока нет добавленных лидов.")
        return
    lines = [_lead_short(lead) for lead in leads]
    await message.answer("Последние лиды:\n\n" + "\n\n".join(lines))


@router.message(Command("search"))
async def cmd_search(message: Message, container, command: CommandObject) -> None:
    query = (command.args or "").strip()
    if not query:
        await message.answer("Использование: /search <название/телефон/instagram/сайт>")
        return
    leads = await container.leads.search_leads(_uid(message), query)
    if not leads:
        await message.answer("Ничего не найдено.")
        return
    lines = [_lead_short(lead) for lead in leads]
    await message.answer("Найдено:\n\n" + "\n\n".join(lines))


@router.message(Command("stats"))
async def cmd_stats(message: Message, container) -> None:
    stats = await container.leads.get_stats(_uid(message))
    await message.answer(
        f"📊 Статистика:\n"
        f"• Лидов всего: {stats['total']}\n"
        f"• За неделю: {stats['week']}\n"
        f"• Объединено дублей: {stats['duplicates']}\n"
        f"• Расход на AI: ${stats['cost_usd']:.6f}"
    )


@router.message(Command("undo"))
async def cmd_undo(message: Message, container) -> None:
    result = await container.leads.undo_last(_uid(message))
    await message.answer(result or "Нечего откатывать.")


@router.message(Command("settings"))
async def cmd_settings(message: Message, container) -> None:
    settings = container.settings
    sheet_link = (
        f"https://docs.google.com/spreadsheets/d/{settings.GOOGLE_SHEET_ID}"
        if settings.GOOGLE_SHEET_ID
        else "не задана"
    )
    await message.answer(
        "⚙️ Настройки:\n"
        f"• Город по умолчанию: {settings.DEFAULT_CITY}\n"
        f"• Уведомления о дублях: включены\n"
        f"• Таблица: {sheet_link}\n\n"
        "Значения задаются через переменные окружения (.env)."
    )


@router.message(Command("resync"))
async def cmd_resync(message: Message, container) -> None:
    leads = await container.leads.get_unsynced_leads(_uid(message))
    if not leads:
        await message.answer("Нет лидов, ожидающих синхронизации.")
        return
    await message.answer(f"Синхронизирую {len(leads)} лид(ов)…")
    done = 0
    for lead in leads:
        row = await container.sheets.sync_lead(lead)
        if row:
            await container.leads.update_lead(lead.id, sheet_row=row)
            done += 1
    await message.answer(f"✅ Синхронизировано: {done}/{len(leads)}.")


# ---------------- collection (buffer) ----------------
@router.message(LeadForm.Collecting, F.text)
async def on_collecting_text(message: Message, state, container) -> None:
    await flow.append_message(container, _uid(message), _chat(message), state, message.text)


@router.message(LeadForm.Reviewing, F.text)
async def on_review_text(message: Message) -> None:
    await message.answer("Используйте кнопки под карточкой: ✅ Добавить / ✏️ Исправить / ❌ Отмена.")


@router.message(LeadForm.ConfirmingDuplicate, F.text)
async def on_duplicate_text(message: Message) -> None:
    await message.answer("Пожалуйста, выберите один из вариантов под сообщением.")


@router.message(LeadForm.EditingField, F.text)
async def on_edit_text(message: Message, state, container) -> None:
    data = await state.get_data()
    field = data.get("editing_field")
    if not field:
        await message.answer("Сначала выберите поле для исправления.")
        return
    extracted = dict(data.get("extracted") or {})
    extracted = flow.apply_edit(extracted, field, message.text)
    session_id = data.get("session_id")
    await state.update_data(extracted=extracted, editing_field=None)
    await flow.show_review(
        container, _chat(message), state, ExtractionResult.model_validate(extracted), session_id
    )


@router.message(LeadForm.ManualEntry, F.text)
async def on_manual_text(message: Message, state, container) -> None:
    await flow.process_manual_message(container, _chat(message), state, message.text)


# Idle: any text starts a new collection.
@router.message(StateFilter(None), F.text)
async def on_idle_text(message: Message, state, container) -> None:
    await flow.start_collection(container, _uid(message), _chat(message), state, message.text)


# ---------------- callbacks ----------------
@router.callback_query(F.data == CB_DONE)
async def cb_done(callback: CallbackQuery, state, container) -> None:
    await callback.answer()
    await flow.finalize_collection(container, _uid(callback), _chat(callback), state)


@router.callback_query(F.data == CB_CANCEL)
async def cb_cancel(callback: CallbackQuery, state, container) -> None:
    await callback.answer()
    await flow.cancel_collection(container, _uid(callback), state, chat_id=_chat(callback))


@router.callback_query(F.data == CB_ADD)
async def cb_add(callback: CallbackQuery, state, container) -> None:
    await callback.answer()
    await flow.confirm_add(container, _uid(callback), _chat(callback), state)


@router.callback_query(F.data == CB_EDIT)
async def cb_edit(callback: CallbackQuery, state) -> None:
    await callback.answer()
    await state.set_state(LeadForm.EditingField)
    await state.update_data(editing_field=None)
    await callback.message.answer("Какое поле исправить?", reply_markup=fields_keyboard())


@router.callback_query(F.data == CB_EDIT_DONE)
async def cb_edit_done(callback: CallbackQuery, state, container) -> None:
    await callback.answer()
    data = await state.get_data()
    extracted = ExtractionResult.model_validate(data.get("extracted") or {})
    session_id = data.get("session_id")
    await flow.show_review(container, _chat(callback), state, extracted, session_id)


@router.callback_query(F.data.startswith(CB_FIELD_PREFIX))
async def cb_select_field(callback: CallbackQuery, state) -> None:
    await callback.answer()
    field = callback.data[len(CB_FIELD_PREFIX):]
    await state.update_data(editing_field=field)
    await callback.message.answer(
        f"Введите новое значение для «{FIELD_LABELS.get(field, field)}»:"
    )


@router.callback_query(F.data == CB_DUP_SAME)
async def cb_dup_same(callback: CallbackQuery, state, container) -> None:
    await callback.answer()
    await flow.handle_duplicate_choice(container, _uid(callback), _chat(callback), state, "same")


@router.callback_query(F.data == CB_DUP_NEW)
async def cb_dup_new(callback: CallbackQuery, state, container) -> None:
    await callback.answer()
    await flow.handle_duplicate_choice(container, _uid(callback), _chat(callback), state, "new")


@router.callback_query(F.data == CB_DUP_CONTACT)
async def cb_dup_contact(callback: CallbackQuery, state, container) -> None:
    await callback.answer()
    await flow.handle_duplicate_choice(
        container, _uid(callback), _chat(callback), state, "contact"
    )


def _lead_short(lead) -> str:
    parts = [f"#{lead.id}"]
    if lead.company_name:
        parts.append(lead.company_name)
    if lead.city:
        parts.append(lead.city)
    if lead.phone:
        parts.append(lead.phone)
    if lead.instagram:
        parts.append(f"@{lead.instagram}")
    if lead.sheet_row:
        parts.append(f"строка {lead.sheet_row}")
    return " — ".join(parts)
