"""Flow orchestration: buffer → extraction → dedup → review → save. Testable without Telegram."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.utils.markdown import html_decoration
from pydantic import ValidationError

from app.bot.cards import render_card, render_lead_summary, sheet_link_html
from app.bot.keyboards import collecting_keyboard, duplicate_keyboard, review_keyboard
from app.bot.premium import emoji
from app.bot.safe import notify
from app.bot.states import LeadForm
from app.logging_config import get_session_id, set_session_id
from app.models import Lead
from app.schemas.extraction import ExtractionResult
from app.services.dedup import fingerprint_from_extraction
from app.services.extraction import AIQuotaExceededError, ExtractionError
from app.services.lead_service import (
    SESSION_STATUS_CANCELLED,
    SESSION_STATUS_EDITING,
    SESSION_STATUS_REVIEW,
)
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

# What the user sees when OpenRouter answers 429/402 (the ~50 requests/day free
# tier or an empty balance). Three facts, in the order that helps: the reason, the
# way out ($10 → 1000 requests/day), and that nothing is lost meanwhile — the
# manual-entry wizard starts right after this message. HTML parse mode, so no raw
# markup may appear here (fixed copy, no interpolation).
AI_LIMIT_MESSAGE = (
    f"{emoji('warning')} Дневной лимит бесплатных AI-запросов исчерпан "
    "(вернётся после 00:00 UTC).\n"
    "Можно пополнить OpenRouter на $10 — лимит станет 1000 запросов/сутки.\n"
    "Пока могу принять лид вручную."
)

# Every field the extraction schema knows; an edit is accepted only for one of them
# and is always validated. Callback data is user-controlled, so the check cannot live
# in the keyboard alone: the audit reproduced a crash with a crafted «field:rating»
# («Рейтинг» = «нет данных») that the visible keyboard does not even offer.
SCHEMA_FIELDS = frozenset(ExtractionResult.model_fields)

# What to tell the user when a value cannot be stored in the field.
FIELD_HINTS: dict[str, str] = {
    "rating": "нужно число, например 4.8",
    "reviews_count": "нужно целое число, например 120",
    "source_guess": "допустимо: 2gis, instagram, website, google, other",
}
DEFAULT_FIELD_HINT = "значение не подходит для этого поля"

# One extraction per finalized buffer. The FSM state alone cannot guarantee this:
# ``get_state``/``set_state`` are two awaits, so a second caller can slip between
# them. This set is only ever touched synchronously (no await in between), which
# makes claiming it atomic inside the event loop.
_FINALIZE_IN_FLIGHT: set[int] = set()


class EditValidationError(Exception):
    """A user-supplied value does not satisfy the extraction schema."""

    def __init__(self, field: str, hint: str | None = None):
        self.field = field
        self.hint = hint or FIELD_HINTS.get(field, DEFAULT_FIELD_HINT)
        super().__init__(f"{field}: {self.hint}")


def _claim_finalize(user_id: int) -> bool:
    if user_id in _FINALIZE_IN_FLIGHT:
        return False
    _FINALIZE_IN_FLIGHT.add(user_id)
    return True


def _release_finalize(user_id: int) -> None:
    _FINALIZE_IN_FLIGHT.discard(user_id)


def is_finalizing(user_id: int) -> bool:
    """True while an extraction for *user_id* is in flight (also a test seam)."""
    return user_id in _FINALIZE_IN_FLIGHT


def bind_session_id(session_id: int | str | None) -> None:
    """Correlate every log line of the current task/context with the lead session."""
    set_session_id(str(session_id) if session_id is not None else None)


async def start_collection(
    container, user_id: int, chat_id: int, state: FSMContext, first_text: str
) -> None:
    """Open a new collecting session and buffer the first message."""
    container.session_buffer.cancel(user_id)
    session_id = await container.leads.create_session(user_id)
    bind_session_id(session_id)
    await state.set_state(LeadForm.Collecting)
    await state.set_data({"session_id": session_id, "combined_text": first_text or ""})
    if first_text:
        await container.leads.add_raw_message(session_id, None, first_text)
    container.session_buffer.schedule(
        user_id, lambda: finalize_collection(container, user_id, chat_id, state)
    )
    await notify(
        container,
        chat_id,
        f"{emoji('collecting')} Собираю лид… отправьте ещё текст или нажмите "
        f"{emoji('check')} Готово.",
        action="collecting_prompt",
        user_id=user_id,
        reply_markup=collecting_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def append_message(
    container, user_id: int, chat_id: int, state: FSMContext, text: str
) -> None:
    """Buffer another message and reset the finalize timer."""
    data = await state.get_data()
    session_id = data.get("session_id")
    bind_session_id(session_id)
    combined = (data.get("combined_text") or "").strip()
    combined = f"{combined}\n{text}".strip() if combined else text
    await state.update_data(combined_text=combined)
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
    bind_session_id(session_id)
    if session_id:
        await container.leads.update_session(session_id, status=SESSION_STATUS_CANCELLED)
    await state.clear()
    if not silent and chat_id is not None:
        await notify(
            container,
            chat_id,
            f"{emoji('cross')} Отменено.",
            action="cancel_notice",
            user_id=user_id,
            parse_mode=ParseMode.HTML,
        )


async def finalize_collection(container, user_id: int, chat_id: int, state: FSMContext) -> None:
    """Run extraction for the buffered text and route to review / dedup / manual entry.

    Re-entry safe. The free models answer in 15–40 s; without the guard a message
    sent meanwhile started a *second* extraction for the same buffer, so the chat
    got two cards and «Добавить» under the first one saved the second lead.
    """
    current = await state.get_state()
    # No await between reading the state and claiming, so exactly one caller wins.
    # Whoever loses is told what is going on instead of starting an extraction.
    if current != LeadForm.Collecting.state or not _claim_finalize(user_id):
        if current in (LeadForm.Collecting.state, LeadForm.Processing.state):
            await notify(
                container,
                chat_id,
                f"{emoji('collecting')} Уже обрабатываю предыдущее сообщение — "
                "подождите пару секунд.",
                action="already_processing",
                user_id=user_id,
                parse_mode=ParseMode.HTML,
            )
        return

    container.session_buffer.cancel(user_id)
    try:
        await _finalize_collection(container, user_id, chat_id, state)
    except Exception:
        # Never leave the user stuck in «обработка»: report and return to Idle.
        logger.exception("finalization failed for user %s", user_id)
        await state.clear()
        await notify(
            container,
            chat_id,
            f"{emoji('warning')} Не получилось обработать сообщение. "
            "Пришлите текст ещё раз.",
            action="finalize_failed",
            user_id=user_id,
            parse_mode=ParseMode.HTML,
        )
    finally:
        _release_finalize(user_id)


async def _still_processing(state: FSMContext) -> bool:
    """Whether this pipeline still owns the dialog (False after /cancel, /new, …)."""
    return await state.get_state() == LeadForm.Processing.state


async def _finalize_collection(container, user_id: int, chat_id: int, state: FSMContext) -> None:
    """The extraction pipeline itself; runs under the finalization claim."""
    # Lock the FSM *before* the slow LLM call: messages that arrive while the model
    # is thinking are answered by the Processing handlers, never extracted again.
    await state.set_state(LeadForm.Processing)
    data = await state.get_data()
    combined = (data.get("combined_text") or "").strip()
    session_id = data.get("session_id")
    if not combined:
        await state.clear()
        return

    # The session stays bound for the whole chain — dedup, the sheet sync task and
    # the FSM writes are part of the same lead (FIX-6).
    previous_session_id = get_session_id()
    bind_session_id(session_id)
    try:
        await container.leads.update_session(
            session_id, combined_text=combined, status=SESSION_STATUS_REVIEW,
            last_message_at=datetime.now(timezone.utc),
        )
        await notify(
            container,
            chat_id,
            f"{emoji('analyzing')} Анализирую…",
            action="analyzing",
            user_id=user_id,
            parse_mode=ParseMode.HTML,
        )

        try:
            if not container.extraction.api_key:
                raise ExtractionError("OPENROUTER_API_KEY не задан")
            extracted = await container.extraction.extract(combined, session_id)
        except ExtractionError as exc:
            if not await _still_processing(state):
                return
            reason = str(exc)
            if isinstance(exc, AIQuotaExceededError):
                # Quota, not an outage: say what happened and hand over to manual
                # entry. The typed text stays in the session, so a lead can still be
                # saved without AI (the wizard starts collecting it below).
                await notify(
                    container,
                    chat_id,
                    AI_LIMIT_MESSAGE,
                    action="ai_limit_reached",
                    user_id=user_id,
                    parse_mode=ParseMode.HTML,
                )
            elif "OPENROUTER_API_KEY" in reason:
                await notify(
                    container,
                    chat_id,
                    f"{emoji('warning')} LLM не настроен (нет OPENROUTER_API_KEY). "
                    "Могу ввести вручную.",
                    action="extraction_not_configured",
                    user_id=user_id,
                    parse_mode=ParseMode.HTML,
                )
            else:
                await notify(
                    container,
                    chat_id,
                    f"{emoji('warning')} Не удалось распознать автоматически. Введём вручную.",
                    action="extraction_failed",
                    user_id=user_id,
                    parse_mode=ParseMode.HTML,
                )
            await start_manual_entry(container, chat_id, state, session_id)
            return

        # The buffer may have been cancelled (/cancel, /new, /start) while the LLM
        # was working: that pipeline is stale and must not touch the dialog again.
        if not await _still_processing(state):
            return

        await container.leads.update_session(session_id, status=SESSION_STATUS_REVIEW)

        if extracted.is_empty():
            await cancel_collection(container, user_id, state, silent=True)
            await notify(
                container,
                chat_id,
                "Не удалось распознать компанию — пришли больше деталей.",
                action="extraction_empty",
                user_id=user_id,
            )
            return

        fingerprint = fingerprint_from_extraction(extracted)
        match = await container.dedup.find_duplicate(fingerprint, user_id)
        if not await _still_processing(state):
            return

        if match is not None and match.level == "strong":
            lead = await container.leads.add_lead(
                user_id, extracted, session_id, merge_target_id=match.lead_id
            )
            # ``lead`` is the dead duplicate row; the merged values live on the target.
            spawn_sync(container, merge_sync_target(lead), chat_id)
            await state.clear()
            await notify(
                container,
                chat_id,
                f"{emoji('check')} Найдено совпадение — обновлён лид #{match.lead_id}.",
                action="duplicate_strong",
                user_id=user_id,
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
            await notify(
                container,
                chat_id,
                text,
                action="duplicate_review",
                user_id=user_id,
                reply_markup=duplicate_keyboard(),
                parse_mode=ParseMode.HTML,
            )
            return

        await show_review(container, chat_id, state, extracted, session_id)
    finally:
        set_session_id(previous_session_id)


async def set_session_status(container, session_id: int | None, status: str) -> None:
    """Write ``lead_sessions.status`` for the current dialog (FIX-25a).

    The status column is what the database says about a dialog when nobody is
    watching the process, so it has to follow every stage instead of staying at
    ``collecting``/``review``: ``editing`` is written while «Исправить» is open, and
    ``review`` again as soon as a card is rendered. No session (e.g. an FSM state
    that lost its data) → nothing to update.
    """
    if session_id is None:
        return
    await container.leads.update_session(session_id, status=status)


async def show_review(
    container, chat_id: int, state: FSMContext, extracted: ExtractionResult, session_id: int
) -> None:
    bind_session_id(session_id)
    await state.set_state(LeadForm.Reviewing)
    await state.update_data(extracted=extracted.model_dump(), session_id=session_id)
    # A card is on screen ⇔ the dialog is waiting for a decision («review»), which
    # also takes it back out of «editing» after a correction.
    await set_session_status(container, session_id, SESSION_STATUS_REVIEW)
    await notify(
        container,
        chat_id,
        render_card(extracted),
        action="review_card",
        reply_markup=review_keyboard(extracted.has_minimum()),
        parse_mode=ParseMode.HTML,
    )


async def confirm_add(container, user_id: int, chat_id: int, state: FSMContext) -> None:
    data = await state.get_data()
    extracted = ExtractionResult.model_validate(data.get("extracted") or {})
    session_id = data.get("session_id")
    # The user's action belongs to the same lead chain as the extraction.
    bind_session_id(session_id)
    if not extracted.has_minimum():
        await notify(
            container,
            chat_id,
            f"{emoji('warning')} Заполните название или хотя бы один контакт "
            f"({emoji('pencil')} Исправить).",
            action="add_missing_contacts",
            user_id=user_id,
            parse_mode=ParseMode.HTML,
        )
        return
    lead = await container.leads.add_lead(user_id, extracted, session_id)
    # FIX-24: one message, not two. The reply is sent by the sync job, which is the
    # first moment both facts exist («ID #N» and «строка M») — see sync_and_notify.
    spawn_sync(container, lead.id, chat_id, notice=lead_added_notice(lead.id))
    await state.clear()


async def handle_duplicate_choice(
    container, user_id: int, chat_id: int, state: FSMContext, choice: str
) -> None:
    data = await state.get_data()
    extracted = ExtractionResult.model_validate(data.get("extracted") or {})
    session_id = data.get("session_id")
    dup = data.get("duplicate") or {}
    existing_id = dup.get("lead_id")
    # The user's action belongs to the same lead chain as the extraction.
    bind_session_id(session_id)

    if choice == "new":
        lead = await container.leads.add_lead(user_id, extracted, session_id)
        spawn_sync(container, lead.id, chat_id, notice=lead_added_notice(lead.id))
        await state.clear()
        return

    prefer_new_contact = choice == "contact"
    lead = await container.leads.add_lead(
        user_id, extracted, session_id,
        merge_target_id=existing_id, prefer_new_contact=prefer_new_contact,
    )
    # Same as the auto-merge branch: only the live target has a sheet_row to update.
    spawn_sync(container, merge_sync_target(lead), chat_id)
    await state.clear()
    await notify(
        container,
        chat_id,
        f"{emoji('check')} Объединено с лидом #{existing_id}.",
        action="duplicate_merged",
        user_id=user_id,
        parse_mode=ParseMode.HTML,
    )


async def start_manual_entry(
    container, chat_id: int, state: FSMContext, session_id: int
) -> None:
    bind_session_id(session_id)
    await state.set_state(LeadForm.ManualEntry)
    await state.update_data(
        session_id=session_id, manual_step="company_name", manual={}
    )
    await notify(
        container,
        chat_id,
        MANUAL_PROMPTS["company_name"],
        action="manual_prompt",
        parse_mode=ParseMode.HTML,
    )


async def process_manual_message(
    container, user_id: int, chat_id: int, state: FSMContext, text: str
) -> None:
    data = await state.get_data()
    step = data.get("manual_step")
    manual = dict(data.get("manual") or {})
    bind_session_id(data.get("session_id"))
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
            await notify(
                container,
                chat_id,
                "Не удалось распознать компанию — пришли больше деталей.",
                action="extraction_empty_manual",
                user_id=user_id,
            )
        return

    await state.update_data(manual=manual, manual_step=next_step)
    await notify(
        container,
        chat_id,
        MANUAL_PROMPTS[next_step],
        action="manual_prompt",
        user_id=user_id,
        parse_mode=ParseMode.HTML,
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
    """Apply a user edit to the extracted-data dict (re-normalizing where needed).

    The result is validated against the schema *before* it reaches the FSM: a value
    the model cannot hold («нет данных» for «Рейтинг», an unknown source) used to
    raise ``ValidationError`` inside the handler, so the user got no reply and the
    dialog was stuck in ``EditingField``. Callers get
    :class:`EditValidationError` instead and keep the previous data intact.
    """
    if field not in SCHEMA_FIELDS:
        raise EditValidationError(field, "неизвестное поле")

    candidate = dict(data)
    if field == "phone_raw":
        candidate["phone_raw"] = value
        candidate["phone_e164"] = normalize_phone(value)
    elif field == "services":
        candidate["services"] = [s.strip() for s in value.split(",") if s.strip()]
    elif field == "tags":
        candidate["tags"] = [s.strip() for s in value.split(",") if s.strip()]
    elif field == "website":
        candidate["website"] = normalize_website(value)
    elif field in ("instagram", "telegram"):
        candidate[field] = normalize_social_handle(value)
    elif field == "source_guess":
        candidate["source_guess"] = value.strip() if value.strip() else None
    else:
        candidate[field] = value

    try:
        validated = ExtractionResult.model_validate(candidate)
    except ValidationError as exc:
        raise EditValidationError(field) from exc
    # Return the canonical dump: the FSM then holds exactly what a card/review would
    # show (e.g. «4.8» for rating, not the raw string the user typed).
    return validated.model_dump()


def merge_sync_target(lead: Lead) -> int:
    """Id of the row that owns the sheet line after a merge.

    ``LeadService.add_lead(merge_target_id=...)`` returns the *duplicate* row: it is
    immediately marked ``duplicate_of_id``/``deleted_at`` and has no ``sheet_row``.
    Syncing it appends a second line for a company already in the table, while the
    real lead's row keeps the pre-merge values. Only the merge target must be synced.
    """
    return lead.duplicate_of_id or lead.id


def lead_added_notice(lead_id: int) -> str:
    """Opening of the single «лид добавлен» message (FIX-24).

    The row number only exists once the sheet write is done, so the sentence is
    completed by the sync job: «✅ Лид добавлен — ID #7, строка 12».
    """
    return f"{emoji('check')} Лид добавлен — ID #{lead_id}"


def spawn_sync(container, lead_id: int, chat_id: int, notice: str | None = None) -> None:
    """Queue the background sheet sync for *lead_id*.

    Goes through the container's task registry instead of a bare
    ``asyncio.create_task``: the reference is kept until the job finishes (a task the
    loop only sees through a local variable can be collected mid-flight), failures are
    visible, and graceful shutdown drains it instead of dropping the sync.

    *notice* is the «лид добавлен» half of the confirmation the job will send; without
    it the job only reports the row when it becomes ready (merge paths, where the
    «обновлён лид #N» message was already sent by the caller).
    """
    container.tasks.spawn(
        sync_and_notify(container, lead_id, chat_id, notice=notice),
        name=f"sheets-sync-{lead_id}",
    )


async def sync_and_notify(container, lead_id: int, chat_id: int, notice: str | None = None) -> None:
    """Background: sync to Sheets and notify about the result. Never blocks the flow.

    The sync stays off the request path on purpose: the webhook awaits the update
    handler, and a slow/hanging Sheets API would delay the HTTP 200 until Telegram
    re-delivers the update (a replayed «Добавить» would save the lead twice).
    """
    try:
        lead = await container.leads.get_lead(lead_id)
        if lead is None:
            return
        row = await container.sheets.sync_lead(lead)
        # FIX-15: link straight to the table when one is configured.
        link = sheet_link_html(container.settings)
        if row:
            await container.leads.update_lead(lead_id, sheet_row=row)
            if notice:
                # FIX-24: the ID and the row in one message, never two.
                text = f"{notice}, строка {row}."
            else:
                text = f"{emoji('row')} Строка #{row} в таблице готова."
            await notify(
                container,
                chat_id,
                text + (f" {link}" if link else ""),
                action="lead_added" if notice else "sync_row_ready",
                parse_mode=ParseMode.HTML,
            )
        else:
            if notice:
                text = (
                    f"{notice}. Синхронизация с таблицей чуть задержится — "
                    "позже выполните /resync."
                )
            else:
                text = (
                    f"{emoji('check')} Добавлено в базу, синхронизация с таблицей чуть "
                    "задержится. Позже выполните /resync."
                )
            await notify(
                container,
                chat_id,
                text,
                action="sync_deferred",
                parse_mode=ParseMode.HTML,
            )
    except Exception:
        logger.exception("sync_and_notify failed for lead %s", lead_id)
