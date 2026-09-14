"""Review-card rendering (HTML + premium emoji) for the extracted lead.

Every message produced here is sent with ``parse_mode=ParseMode.HTML``, so every
dynamic value (company name, city, phone, … — i.e. anything the LLM or the user
can influence) MUST go through ``html_decoration.quote``. Forgetting one turns
the message into a Telegram 400 "can't parse entities".
"""
from __future__ import annotations

from aiogram.utils.markdown import html_decoration

from app.bot.premium import emoji
from app.models import Lead
from app.schemas.extraction import ExtractionResult


def _fmt(value) -> str:
    return str(value) if value not in (None, "") else "—"


def _q(value) -> str:
    """Escape a dynamic value for HTML output."""
    return html_decoration.quote(str(value))


def render_card(data: ExtractionResult) -> str:
    lines: list[str] = []
    if data.company_name:
        lines.append(f"{emoji('company')} {_q(data.company_name)}")
    if data.city:
        lines.append(f"{emoji('city')} {_q(data.city)}")
    if data.phone_e164 or data.phone_raw:
        lines.append(f"{emoji('phone')} {_q(data.phone_e164 or data.phone_raw)}")
    if data.instagram:
        lines.append(f"{emoji('instagram')} @{_q(data.instagram)}")
    if data.telegram:
        lines.append(f"{emoji('telegram')} @{_q(data.telegram)}")
    if data.website:
        lines.append(f"{emoji('website')} {_q(data.website)}")
    if data.email:
        lines.append(f"{emoji('email')} {_q(data.email)}")
    if data.address:
        lines.append(f"{emoji('address')} {_q(data.address)}")
    if data.category:
        lines.append(f"{emoji('category')} {_q(data.category)}")
    if data.services:
        lines.append(f"{emoji('services')} {_q(', '.join(str(s) for s in data.services))}")
    if data.rating is not None:
        rating = f"{emoji('rating')} {_q(data.rating)}"
        if data.reviews_count is not None:
            rating += f" ({emoji('reviews')} {_q(data.reviews_count)} отзывов)"
        lines.append(rating)
    if data.contact_person:
        lines.append(f"{emoji('contact')} {_q(data.contact_person)}")
    if data.description:
        lines.append(f"{emoji('description')} {_q(data.description)}")

    warnings: list[str] = []
    if not data.company_name:
        warnings.append("не найдено название")
    if not data.has_contact():
        warnings.append("не найден ни один контакт (телефон/email/instagram/сайт)")
    for field in data.uncertain_fields:
        if field not in warnings:
            warnings.append(f"не уверен в поле: {field}")
    if warnings:
        lines.append("")
        lines.append(f"{emoji('warning')} " + "; ".join(_q(w) for w in warnings))

    lines.append("")
    lines.append("Добавить в таблицу?")
    return "\n".join(lines)


def render_lead_summary(lead: Lead) -> str:
    """Compact summary of an existing lead (used for duplicate comparison)."""
    parts = [f"#{lead.id}"]
    if lead.company_name:
        parts.append(f"{emoji('company')} {_q(lead.company_name)}")
    if lead.city:
        parts.append(f"{emoji('city')} {_q(lead.city)}")
    if lead.phone:
        parts.append(f"{emoji('phone')} {_q(lead.phone)}")
    if lead.instagram:
        parts.append(f"{emoji('instagram')} @{_q(lead.instagram)}")
    if lead.website:
        parts.append(f"{emoji('website')} {_q(lead.website)}")
    return "\n".join(parts)
