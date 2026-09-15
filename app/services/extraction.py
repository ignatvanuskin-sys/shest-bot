"""AI extraction service: OpenRouter → strict JSON → Pydantic, with retry + fallback."""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.logging_config import log_json
from app.models import ExtractionLog
from app.schemas.extraction import ExtractionResult

logger = logging.getLogger(__name__)

# Default models verified against the live OpenRouter free-tier catalog.
# They are overridable via OPENROUTER_MODEL / OPENROUTER_FALLBACK_MODEL.
PRIMARY_MODEL = "openrouter/free"
FALLBACK_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"

OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"

# Approximate pricing (USD per 1 token) for non-:free models, used only as a
# fallback when the API response does not include ``usage.cost``.
PRICING: dict[str, dict[str, float]] = {
    "google/gemini-2.0-flash-001": {"in": 0.10 / 1_000_000, "out": 0.40 / 1_000_000},
    "openai/gpt-4o-mini": {"in": 0.15 / 1_000_000, "out": 0.60 / 1_000_000},
}

JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "company_name": {"type": ["string", "null"]},
        "category": {"type": ["string", "null"]},
        "city": {"type": ["string", "null"]},
        "address": {"type": ["string", "null"]},
        "phone_raw": {"type": ["string", "null"]},
        "phone_e164": {"type": ["string", "null"]},
        "has_whatsapp": {"type": ["boolean", "null"]},
        "whatsapp_number": {"type": ["string", "null"]},
        "email": {"type": ["string", "null"]},
        "instagram": {"type": ["string", "null"]},
        "telegram": {"type": ["string", "null"]},
        "website": {"type": ["string", "null"]},
        "services": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
        "description": {"type": ["string", "null"]},
        "rating": {"type": ["number", "null"]},
        "reviews_count": {"type": ["integer", "null"]},
        "contact_person": {"type": ["string", "null"]},
        "source_guess": {"type": ["string", "null"], "enum": ["2gis", "instagram", "website", "google", "other", None]},
        "source_url": {"type": ["string", "null"]},
        "uncertain_fields": {"type": "array", "items": {"type": "string"}},
        "extraction_notes": {"type": ["string", "null"]},
    },
    "required": [],
}

SYSTEM_PROMPT = """Ты — модуль извлечения фактов для CRM-бота LeadForge AI.
Твоя задача: из входящего текста (copy-paste из 2GIS, Instagram, сайта, Google) извлечь
структурированные данные о компании. Ты ИЗВЛЕКАЕШЬ ФАКТЫ, НЕ СОЧИНЯЕШЬ.

Жёсткие правила:
1. Никогда не придумывай отсутствующие данные. Если поле не определяется — ставь null,
   а не пустую строку и не догадку.
2. Всё, в чём ты не уверен, перечисли в uncertain_fields (имена полей из схемы).
3. Телефоны нормализуй в E.164 в phone_e164 (дефолтный регион Казахстан, +7). Исходное
   написание сохраняй в phone_raw. Если есть WhatsApp — has_whatsapp=true и whatsapp_number.
4. Instagram/Telegram сохраняй как чистый username без "@" и без домена.
5. Ссылки очищай от utm_*/fbclid и подобных параметров.
6. source_guess — одно из: 2gis | instagram | website | google | other | null.
7. services и tags — массивы строк (может быть пустой массив).

Верни СТРОГО валидный JSON без markdown-обёртки, без комментариев и без текста вокруг.
Соответствуй следующей JSON-схеме:
""" + json.dumps(JSON_SCHEMA, ensure_ascii=False) + """

Пример нормального ввода:
"ТОО «Ali Motors», Караганда, ул. Крылова 12, +7 700 123 45 67, Instagram: @alimotors,
Автосервис — ремонт ходовой части, рейтинг 4.8, 120 отзывов, сайт https://alimotors.kz/?utm_source=2gis"
Ответ:
{"company_name": "Ali Motors", "category": "Автосервис", "city": "Караганда",
 "address": "ул. Крылова 12", "phone_raw": "+7 700 123 45 67", "phone_e164": "+77001234567",
 "has_whatsapp": null, "whatsapp_number": null, "email": null, "instagram": "alimotors",
 "telegram": null, "website": "https://alimotors.kz/", "services": ["ремонт ходовой части"],
 "tags": [], "description": null, "rating": 4.8, "reviews_count": 120, "contact_person": null,
 "source_guess": "2gis", "source_url": null, "uncertain_fields": [], "extraction_notes": null}

Пример «мусорного» ввода:
"привет как дела вообще"
Ответ:
{"company_name": null, "category": null, "city": null, "address": null, "phone_raw": null,
 "phone_e164": null, "has_whatsapp": null, "whatsapp_number": null, "email": null,
 "instagram": null, "telegram": null, "website": null, "services": [], "tags": [],
 "description": null, "rating": null, "reviews_count": null, "contact_person": null,
 "source_guess": null, "source_url": null, "uncertain_fields": [], "extraction_notes": null}
"""


class ExtractionError(Exception):
    """Raised when extraction fails after all retries/fallback attempts."""


class _InvalidJsonError(Exception):
    """LLM returned content that could not be parsed/validated."""


class _UnavailableError(Exception):
    """LLM provider unreachable / timeout / non-2xx."""


def _extract_json_object(content: str) -> dict[str, Any]:
    """Parse an LLM reply into a dict, tolerating markdown code fences."""
    text = content.strip()
    # Strip ```json ... ``` fences.
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    # Take the outermost {...} block if the model added prose around it.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _InvalidJsonError(f"invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise _InvalidJsonError("JSON root is not an object")
    return parsed


def parse_extraction_content(content: str) -> ExtractionResult:
    """Parse raw LLM content into an ``ExtractionResult`` (raises on failure)."""
    parsed = _extract_json_object(content)
    try:
        return ExtractionResult.model_validate(parsed)
    except Exception as exc:  # pydantic ValidationError
        raise _InvalidJsonError(f"validation error: {exc}") from exc


class ExtractionService:
    def __init__(
        self,
        api_key: str,
        session_factory: async_sessionmaker,
        primary_model: str = PRIMARY_MODEL,
        fallback_model: str = FALLBACK_MODEL,
    ):
        self.api_key = api_key
        self.session_factory = session_factory
        self.primary_model = primary_model
        self.fallback_model = fallback_model
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0),
            headers={
                "Authorization": f"Bearer {api_key}" if api_key else "",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://leadforge.local",
                "X-Title": "LeadForge AI",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def extract(self, text: str, session_id: int | None = None) -> ExtractionResult:
        """Run extraction with retry + model fallback. Raises ExtractionError if all fail."""
        if not self.api_key:
            raise ExtractionError("OPENROUTER_API_KEY не задан")

        attempts: list[tuple[str, str | None]] = [
            (self.primary_model, None),
            (self.primary_model, None),  # one retry on the same provider
            (self.fallback_model, None),
        ]
        error_note: str | None = None
        last_error: Exception | None = None

        for model, _ in attempts:
            try:
                result = await self._attempt(model, text, session_id, error_note)
                log_json(
                    logger, 20, "extraction succeeded",
                    session_id=session_id, model=model, success=True,
                )
                return result
            except _InvalidJsonError as exc:
                last_error = exc
                error_note = str(exc)  # retry with the exact validation error
            except _UnavailableError as exc:
                last_error = exc
                error_note = None

        raise ExtractionError(f"извлечение не удалось: {last_error}")

    async def _attempt(
        self, model: str, text: str, session_id: int | None, error_note: str | None
    ) -> ExtractionResult:
        messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        user_content = f"Текст для извлечения:\n{text}"
        if error_note:
            user_content += (
                f"\n\nПрошлый ответ не прошёл валидацию. Ошибка: {error_note}\n"
                "Исправь и верни строго валидный JSON по схеме."
            )
        messages.append({"role": "user", "content": user_content})

        payload = {
            "model": model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "temperature": 0,
        }

        started = time.perf_counter()
        try:
            response = await self._client.post(OPENROUTER_CHAT_URL, json=payload)
        except httpx.HTTPError as exc:
            latency = int((time.perf_counter() - started) * 1000)
            await self._log_attempt(model, session_id, None, None, None, latency, False, str(exc))
            raise _UnavailableError(f"network error: {exc}") from exc

        latency = int((time.perf_counter() - started) * 1000)

        if response.status_code >= 400:
            await self._log_attempt(model, session_id, None, None, None, latency, False, response.text[:500])
            raise _UnavailableError(f"HTTP {response.status_code}: {response.text[:200]}")

        # A 200 with a non-JSON/empty body (HTML error page, truncated stream,
        # proxy answer) is a *provider* failure, not a crash: it must join the
        # normal retry → fallback → manual-entry chain instead of escaping as a
        # bare ValueError, which left the user with no card and no prompt.
        try:
            data = response.json()
        except ValueError as exc:
            await self._log_attempt(
                model, session_id, None, None, None, latency, False, f"non-JSON body: {exc}"
            )
            raise _UnavailableError(f"response body is not JSON: {exc}") from exc

        if not isinstance(data, dict):
            await self._log_attempt(
                model, session_id, None, None, None, latency, False, "non-object body"
            )
            raise _UnavailableError("response body is not a JSON object")

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            await self._log_attempt(model, session_id, None, None, None, latency, False, "empty choices")
            raise _UnavailableError("unexpected response shape") from exc

        if not isinstance(content, str) or not content.strip():
            # 200 with no completion (``content: null``) — nothing to parse.
            await self._log_attempt(model, session_id, None, None, None, latency, False, "empty content")
            raise _UnavailableError("empty completion content")

        usage = data.get("usage") or {}
        tokens_in = usage.get("prompt_tokens")
        tokens_out = usage.get("completion_tokens")
        cost = self._estimate_cost(model, tokens_in, tokens_out, usage)

        try:
            result = parse_extraction_content(content)
        except _InvalidJsonError as exc:
            await self._log_attempt(model, session_id, tokens_in, tokens_out, cost, latency, False, str(exc))
            raise

        await self._log_attempt(model, session_id, tokens_in, tokens_out, cost, latency, True, None)
        return result

    @staticmethod
    def _estimate_cost(
        model: str,
        tokens_in: int | None,
        tokens_out: int | None,
        usage: dict[str, Any] | None = None,
    ) -> float | None:
        # Free models on OpenRouter always cost nothing.
        if model.endswith(":free"):
            return 0.0
        # Prefer the provider-reported cost when present.
        usage = usage or {}
        if usage.get("cost") is not None:
            try:
                return float(usage["cost"])
            except (TypeError, ValueError):
                pass
        if tokens_in is None or tokens_out is None:
            return None
        price = PRICING.get(model)
        if not price:
            return None
        return round(tokens_in * price["in"] + tokens_out * price["out"], 8)

    async def _log_attempt(
        self,
        model: str,
        session_id: int | None,
        tokens_in: int | None,
        tokens_out: int | None,
        cost: float | None,
        latency_ms: int | None,
        success: bool,
        error: str | None,
    ) -> None:
        log_json(
            logger, 20, "llm attempt",
            session_id=session_id, model=model, tokens_in=tokens_in, tokens_out=tokens_out,
            cost_usd_est=cost, latency_ms=latency_ms, success=success,
        )
        try:
            async with self.session_factory() as session:
                session.add(
                    ExtractionLog(
                        session_id=session_id,
                        model=model,
                        tokens_in=tokens_in,
                        tokens_out=tokens_out,
                        cost_usd_est=cost,
                        latency_ms=latency_ms,
                        success=success,
                        error=error,
                    )
                )
                await session.commit()
        except Exception:  # logging must never break the extraction flow
            logger.exception("failed to persist extraction_log")
