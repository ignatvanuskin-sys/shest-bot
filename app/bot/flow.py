"""Flow orchestration: buffer → extraction → dedup → review → save. Testable without Telegram."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.utils.markdown import html_decoration

from app.bot.cards import render_card, render_lead_summary
from app.bot.keyboards import collecting_keyboard, duplicate_keyboard, review_keyboard
from app.bot.premium import emoji
from app.bot.states import LeadForm
from app.logging_config import set_session_id
from app.schemas.extraction import ExtractionResult
from app.services.dedup import fingerprint_from_extraction
from app.services.extraction import ExtractionError
from app.services.normalize import normalize_phone, normalize_social_handle, normalize_website

logger = logging.getLogger(__name__)

MANUAL_STEPS = ("company_name", "phone", "city", "website", "instagram")
# HTML: sent with parse_mode=HTML, so the prompts must never contain raw markup.
MANUAL_PROMPTS = {
    "company_name": f"{emoji('company')} Введите название компании:",
    "phone": f"{emoji('phone')} Введите телефон (или «-», чтобы пропустить):",
    "city": f"{emoji('city')} Введите город (или «-»):",
    "website": f"{emoji('website')} Введите сайт (или «-»):",
    "instagram": f"{emoji('instagram')} Введите Instagram (или «-»):",
}


async def start_collection(
    container, user_id: int, chat_id: int, state: FSMContext, first_text: str
) -> None:
    """Open a new collecting session and buffer the first message."""
    container.session_buffer.cancel(user_id)
    session_id = await container.leads.create_session(user_id)
    await state.set_state(LeadForm.Collecting)
    await state.set_data({"session_id": session_id, "combined_text": first_text or ""})
    if first_text:
        await container.leads.add_raw_message(session_id, None, first_text)
    container.session_buffer.schedule(
        user_id, lambda: finalize_collection(container, user_id, chat_id, state)
    )
    await container.bot.send_message(
        chat_id,
        f"{emoji('collecting')} Собираю лид… отправьте ещё текст или нажмите "
        f"{emoji('check')} Готово.",
        reply_markup=collecting_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def append_message(
    container, user_id: int, chat_id: int, state: FSMContext, text: str
) -> None:
    """Buffer another message and reset the finalize timer."""
    data = await state.get_data()
    combined = (data.get("combined_text") or "").strip()
    combined = f"{combined}\n{text}".strip() if combined else text
    await state.update_data(combined_text=combined)
    session_id = data.get("session_id")
    await container.leads.add_raw_message(session_id, None, text)
    await container.leads.update_session(
        session_id, last_message_at=datetime.now(timezone.utc)
    )
    container.session_buffer.schedule(
        user_id, lambda: finalize_collection(container, user_id, chat_id, state)
    )


async def cancel_collection(
    container, user_id: int, state: FSMContext, silent: bool = False, chat_id: int | None = None
) -> None:
    """Cancel the current collection and return to Idle."""
    container.session_buffer.cancel(user_id)
    data = await state.get_data()
    session_id = data.get("session_id")
    if session_id:
        await container.leads.update_session(session_id, status="cancelled")
    await state.clear()
    if not silent and chat_id is not None:
        await container.bot.send_message(
            chat_id, f"{emoji('cross')} Отменено.", parse_mode=ParseMode.HTML
        )


async def finalize_collection(container, user_id: int, chat_id: int, state: FSMContext) -> None:
    """Run extraction for the buffered text and route to review / dedup / manual entry."""
    current = await state.get_state()
    if current != LeadForm.Collecting.state:
        return
    container.session_buffer.cancel(user_id)
    data = await state.get_data()
    combined = (data.get("combined_text") or "").strip()
    session_id = data.get("session_id")
    if not combined:
        await state.clear()
        return

    await container.leads.update_session(
        session_id, combined_text=combined, status="review",
        last_message_at=datetime.now(timezone.utc),
    )
    await container.bot.send_message(
        chat_id, f"{emoji('analyzing')} Анализирую…", parse_mode=ParseMode.HTML
    )

    set_session_id(str(session_id))
    try:
        if not container.extraction.api_key:
            raise ExtractionError("OPENROUTER_API_KEY не задан")
        extracted = await container.extraction.extract(combined, session_id)
    except ExtractionError as exc:
        reason = str(exc)
        if "OPENROUTER_API_KEY" in reason:
            await container.bot.send_message(
                chat_id,
                f"{emoji('warning')} LLM не настроен (нет OPENROUTER_API_KEY). "
                "Могу ввести вручную.",
                parse_mode=ParseMode.HTML,
            )
        else:
            await container.bot.send_message(
                chat_id,
                f"{emoji('warning')} Не удалось распознать автоматически. Введём вручную.",
                parse_mode=ParseMode.HTML,
            )
        await start_manual_entry(container, chat_id, state, session_id)
        set_session_id(None)
        return
    finally:
        set_session_id(None)

    await container.leads.update_session(session_id, status="review")

    if extracted.is_empty():
        await cancel_collection(container, user_id, state, silent=True)
        await container.bot.send_message(
            chat_id, "Не удалось распознать компанию — пришли больше деталей."
        )
        return

    fingerprint = fingerprint_from_extraction(extracted)
    match = await container.dedup.find_duplicate(fingerprint, user_id)

    if match is not None and match.level == "strong":
        lead = await container.leads.add_lead(
            user_id, extracted, session_id, merge_target_id=match.lead_id
        )
        asyncio.create_task(sync_and_notify(container, lead.id, chat_id))
        await state.clear()
        await container.bot.send_message(
            chat_id,
            f"{emoji('check')} Найдено совпадение — обновлён лид #{match.lead_id}.",
            parse_mode=ParseMode.HTML,
        )
        return

    if match is not None and match.level == "medium":
        existing = await container.leads.get_lead(match.lead_id)
        await state.set_state(LeadForm.ConfirmingDuplicate)
        await state.update_data(
            extracted=extracted.model_dump(),
            session_id=session_id,
            duplicate={"lead_id": match.lead_id, "reason": match.reason, "score": match.score},
        )
        text = (
            f"{emoji('warning')} Похоже на уже существующий лид "
            f"({html_decoration.quote(str(match.reason))}).\n\n"
            f"Новая карточка:\n{render_card(extracted)}\n\n"
            f"Существующий лид:\n"
            f"{render_lead_summary(existing) if existing else '#' + str(match.lead_id)}"
        )
        await container.bot.send_message(
            chat_id, text, reply_markup=duplicate_keyboard(), parse_mode=ParseMode.HTML
        )
        return

    await show_review(container, chat_id, state, extracted, session_id)


async def show_review(
    container, chat_id: int, state: FSMContext, extracted: ExtractionResult, session_id: int
) -> None:
    await state.set_state(LeadForm.Reviewing)
    await state.update_data(extracted=extracted.model_dump(), session_id=session_id)
    await container.bot.send_message(
        chat_id,
        render_card(extracted),
        reply_markup=review_keyboard(extracted.has_minimum()),
        parse_mode=ParseMode.HTML,
    )


async def confirm_add(container, user_id: int, chat_id: int, state: FSMContext) -> None:
    data = await state.get_data()
    extracted = ExtractionResult.model_validate(data.get("extracted") or {})
    session_id = data.get("session_id")
    if not extracted.has_minimum():
        await container.bot.send_message(
            chat_id,
            f"{emoji('warning')} Заполните название или хотя бы один контакт "
            f"({emoji('pencil')} Исправить).",
            parse_mode=ParseMode.HTML,
        )
        return
    lead = await container.leads.add_lead(user_id, extracted, session_id)
    asyncio.create_task(sync_and_notify(container, lead.id, chat_id))
    await state.clear()
    await container.bot.send_message(
        chat_id, f"{emoji('check')} Лид добавлен — ID #{lead.id}.", parse_mode=ParseMode.HTML
    )


async def handle_duplicate_choice(
    container, user_id: int, chat_id: int, state: FSMContext, choice: str
) -> None:
    data = await state.get_data()
    extracted = ExtractionResult.model_validate(data.get("extracted") or {})
    session_id = data.get("session_id")
    dup = data.get("duplicate") or {}
    existing_id = dup.get("lead_id")

    if choice == "new":
        lead = await container.leads.add_lead(user_id, extracted, session_id)
        asyncio.create_task(sync_and_notify(container, lead.id, chat_id))
        await state.clear()
        await container.bot.send_message(
            chat_id,
            f"{emoji('check')} Лид добавлен — ID #{lead.id}.",
            parse_mode=ParseMode.HTML,
        )
        return

    prefer_new_contact = choice == "contact"
    lead = await container.leads.add_lead(
        user_id, extracted, session_id,
        merge_target_id=existing_id, prefer_new_contact=prefer_new_contact,
    )
    asyncio.create_task(sync_and_notify(container, lead.id, chat_id))
    await state.clear()
    await container.bot.send_message(
        chat_id,
        f"{emoji('check')} Объединено с лидом #{existing_id}.",
        parse_mode=ParseMode.HTML,
    )


async def start_manual_entry(
    container, chat_id: int, state: FSMContext, session_id: int
) -> None:
    await state.set_state(LeadForm.ManualEntry)
    await state.update_data(
        session_id=session_id, manual_step="company_name", manual={}
    )
    await container.bot.send_message(
        chat_id, MANUAL_PROMPTS["company_name"], parse_mode=ParseMode.HTML
    )


async def process_manual_message(
    container, user_id: int, chat_id: int, state: FSMContext, text: str
) -> None:
    data = await state.get_data()
    step = data.get("manual_step")
    manual = dict(data.get("manual") or {})
    value = text.strip()
    if value == "-":
        value = ""
    manual[step] = value

    idx = MANUAL_STEPS.index(step)
    next_step = MANUAL_STEPS[idx + 1] if idx + 1 < len(MANUAL_STEPS) else None

    if next_step is None:
        extracted = build_manual_result(manual, container.settings.DEFAULT_CITY)
        session_id = data.get("session_id")
        if extracted.has_minimum():
            await show_review(container, chat_id, state, extracted, session_id)
        else:
            await cancel_collection(container, user_id, state, silent=True)
            await container.bot.send_message(
                chat_id, "Не удалось распознать компанию — пришли больше деталей."
            )
        return

    await state.update_data(manual=manual, manual_step=next_step)
    await container.bot.send_message(
        chat_id, MANUAL_PROMPTS[next_step], parse_mode=ParseMode.HTML
    )


def build_manual_result(manual: dict, default_city: str) -> ExtractionResult:
    phone_raw = manual.get("phone") or None
    city = manual.get("city") or None
    return ExtractionResult(
        company_name=manual.get("company_name") or None,
        city=city or default_city,
        phone_raw=phone_raw,
        phone_e164=normalize_phone(phone_raw),
        website=normalize_website(manual.get("website")),
        instagram=normalize_social_handle(manual.get("instagram")),
        source_guess="manual",
        uncertain_fields=[],
    )


def apply_edit(data: dict, field: str, value: str) -> dict:
    """Apply a user edit to the extracted-data dict (re-normalizing where needed)."""
    if field == "phone_raw":
        data["phone_raw"] = value
        data["phone_e164"] = normalize_phone(value)
    elif field == "services":
        data["services"] = [s.strip() for s in value.split(",") if s.strip()]
    elif field == "tags":
        data["tags"] = [s.strip() for s in value.split(",") if s.strip()]
    elif field == "website":
        data["website"] = normalize_website(value)
    elif field in ("instagram", "telegram"):
        data[field] = normalize_social_handle(value)
    elif field == "source_guess":
        data["source_guess"] = value.strip() if value.strip() else None
    else:
        data[field] = value
    return data


async def sync_and_notify(container, lead_id: int, chat_id: int) -> None:
    """Background: sync to Sheets and notify about the result. Never blocks the flow."""
    try:
        lead = await container.leads.get_lead(lead_id)
        if lead is None:
            return
        row = await container.sheets.sync_lead(lead)
        if row:
            await container.leads.update_lead(lead_id, sheet_row=row)
            await container.bot.send_message(
                chat_id,
                f"{emoji('row')} Строка #{row} в таблице готова.",
                parse_mode=ParseMode.HTML,
            )
        else:
            await container.bot.send_message(
                chat_id,
                f"{emoji('check')} Добавлено в базу, синхронизация с таблицей чуть "
                "задержится. Позже выполните /resync.",
                parse_mode=ParseMode.HTML,
            )
    except Exception:
        logger.exception("sync_and_notify failed for lead %s", lead_id)
