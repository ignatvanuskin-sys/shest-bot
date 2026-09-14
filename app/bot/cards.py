"""Review-card rendering (emoji format) for the extracted lead."""
from __future__ import annotations

from app.models import Lead
from app.schemas.extraction import ExtractionResult


def _fmt(value) -> str:
    return str(value) if value not in (None, "") else "—"


def render_card(data: ExtractionResult) -> str:
    lines: list[str] = []
    if data.company_name:
        lines.append(f"🏢 {data.company_name}")
    if data.city:
        lines.append(f"📍 {data.city}")
    if data.phone_e164 or data.phone_raw:
        lines.append(f"📞 {data.phone_e164 or data.phone_raw}")
    if data.instagram:
        lines.append(f"📸 @{data.instagram}")
    if data.telegram:
        lines.append(f"✈️ @{data.telegram}")
    if data.website:
        lines.append(f"🌐 {data.website}")
    if data.email:
        lines.append(f"✉️ {data.email}")
    if data.address:
        lines.append(f"🏠 {data.address}")
    if data.category:
        lines.append(f"🏷️ {data.category}")
    if data.services:
        lines.append(f"🔧 {', '.join(data.services)}")
    if data.rating is not None:
        rating = f"⭐ {data.rating}"
        if data.reviews_count is not None:
            rating += f" ({data.reviews_count} отзывов)"
        lines.append(rating)
    if data.contact_person:
        lines.append(f"👤 {data.contact_person}")
    if data.description:
        lines.append(f"📝 {data.description}")

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
        lines.append("⚠️ " + "; ".join(warnings))

    lines.append("")
    lines.append("Добавить в таблицу?")
    return "\n".join(lines)


def render_lead_summary(lead: Lead) -> str:
    """Compact summary of an existing lead (used for duplicate comparison)."""
    parts = [f"#{lead.id}"]
    if lead.company_name:
        parts.append(f"🏢 {lead.company_name}")
    if lead.city:
        parts.append(f"📍 {lead.city}")
    if lead.phone:
        parts.append(f"📞 {lead.phone}")
    if lead.instagram:
        parts.append(f"📸 @{lead.instagram}")
    if lead.website:
        parts.append(f"🌐 {lead.website}")
    return "\n".join(parts)
