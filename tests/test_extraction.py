"""Extraction tests: JSON parsing/validation + retry/fallback logic."""
from __future__ import annotations

import json
import logging

import httpx
import pytest
from sqlalchemy import select

from app.models import RawMessage
from app.schemas.extraction import ExtractionResult
from app.services.extraction import (
    FALLBACK_MODEL,
    PRIMARY_MODEL,
    AIQuotaExceededError,
    ExtractionError,
    ExtractionService,
    _InvalidJsonError,
    parse_extraction_content,
)
from tests.integration_harness import (
    assert_valid_telegram_html,
    harness,  # noqa: F401  — imported fixture
    no_network,  # noqa: F401  — autouse imported fixture (blocks non-loopback sockets)
)


class _FakeResponse:
    def __init__(
        self,
        content: str | None = None,
        status_code: int = 200,
        data: dict | None = None,
        json_error: Exception | None = None,
    ):
        self.status_code = status_code
        self._content = content
        self._data = data
        self._json_error = json_error
        self.text = content or ""

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._data if self._data is not None else {}


class _FakeClient:
    def __init__(self, responses: list):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def post(self, url, json=None):
        self.calls.append(json)
        if not self.responses:
            return _FakeResponse(content="{}")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def aclose(self):
        pass


class _FakeSession:
    def add(self, obj):
        pass

    async def commit(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSessionFactory:
    def __call__(self):
        return _FakeSession()


def _ok(content: str) -> _FakeResponse:
    return _FakeResponse(
        content=content,
        data={
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


def _non_json(body: str = "<html><body>502 Bad Gateway</body></html>") -> _FakeResponse:
    """HTTP 200 whose body is not JSON (proxy/HTML error page, truncated stream)."""
    return _FakeResponse(
        content=body, json_error=json.JSONDecodeError("Expecting value", body, 0)
    )


def _empty_body() -> _FakeResponse:
    return _FakeResponse(content="", json_error=json.JSONDecodeError("Expecting value", "", 0))


def _null_content() -> _FakeResponse:
    return _FakeResponse(content="", data={"choices": [{"message": {"content": None}}]})


def _http_error(status: int, body: str | None = None) -> _FakeResponse:
    """A non-2xx answer. The default body is what OpenRouter sends for its limits."""
    return _FakeResponse(
        content=body
        or json.dumps(
            {"error": {"message": "rate limit exceeded: free-models-per-day", "code": status}}
        ),
        status_code=status,
    )


# ---------------- parsing ----------------
def test_parse_valid_json():
    result = parse_extraction_content('{"company_name": "Ali Motors", "city": "Караганда"}')
    assert isinstance(result, ExtractionResult)
    assert result.company_name == "Ali Motors"
    assert result.city == "Караганда"


def test_parse_code_fenced_json():
    result = parse_extraction_content('```json\n{"company_name": "Ali"}\n```')
    assert result.company_name == "Ali"


def test_parse_invalid_json_raises():
    with pytest.raises(_InvalidJsonError):
        parse_extraction_content("not json at all")


def test_parse_validation_error_raises():
    with pytest.raises(_InvalidJsonError):
        parse_extraction_content('{"rating": "not-a-number"}')


@pytest.mark.asyncio
async def test_extract_without_api_key():
    service = ExtractionService("", _FakeSessionFactory())
    with pytest.raises(ExtractionError) as exc:
        await service.extract("some text")
    assert "OPENROUTER_API_KEY" in str(exc.value)


# ---------------- retry / fallback ----------------
@pytest.mark.asyncio
async def test_invalid_json_then_retry_succeeds():
    client = _FakeClient([_ok("bad json"), _ok('{"company_name": "Ali"}')])
    service = ExtractionService("key", _FakeSessionFactory())
    service._client = client

    result = await service.extract("text")
    assert result.company_name == "Ali"
    # Two attempts, both on the primary model.
    assert len(client.calls) == 2
    assert client.calls[0]["model"] == PRIMARY_MODEL
    assert client.calls[1]["model"] == PRIMARY_MODEL
    # The retry includes the validation error note.
    assert "Ошибка" in client.calls[1]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_unavailable_then_fallback():
    client = _FakeClient(
        [
            httpx.ConnectError("down"),
            httpx.ConnectError("down"),
            _ok('{"company_name": "Fallback Co"}'),
        ]
    )
    service = ExtractionService("key", _FakeSessionFactory())
    service._client = client

    result = await service.extract("text")
    assert result.company_name == "Fallback Co"
    assert client.calls[0]["model"] == PRIMARY_MODEL
    assert client.calls[1]["model"] == PRIMARY_MODEL
    assert client.calls[2]["model"] == FALLBACK_MODEL


@pytest.mark.asyncio
async def test_all_attempts_fail_raises():
    client = _FakeClient([httpx.ConnectError("down")] * 3)
    service = ExtractionService("key", _FakeSessionFactory())
    service._client = client

    with pytest.raises(ExtractionError):
        await service.extract("text")


# ---------------- FIX-2: a malformed 200 body is a *provider* error ----------------
# ``response.json()`` used to sit outside every ``except``: a 200 with a non-JSON or
# empty body raised a bare ValueError that flew past ExtractionError, the global
# error handler swallowed the update and the user got neither a card nor the manual
# entry prompt. It must join the retry → fallback → manual chain instead.
@pytest.mark.asyncio
async def test_non_json_body_is_retried_instead_of_crashing():
    client = _FakeClient([_non_json(), _ok('{"company_name": "Ali"}')])
    service = ExtractionService("key", _FakeSessionFactory())
    service._client = client

    result = await service.extract("text")  # must not raise ValueError

    assert result.company_name == "Ali"
    assert len(client.calls) == 2, "the non-JSON answer was not retried"
    assert client.calls[0]["model"] == PRIMARY_MODEL


@pytest.mark.asyncio
async def test_non_json_body_on_every_attempt_raises_extraction_error():
    client = _FakeClient([_non_json()] * 3)
    service = ExtractionService("key", _FakeSessionFactory())
    service._client = client

    with pytest.raises(ExtractionError):  # not ValueError — the flow catches this
        await service.extract("text")

    assert len(client.calls) == 3, "primary + retry + fallback must all be tried"
    assert client.calls[2]["model"] == FALLBACK_MODEL


@pytest.mark.asyncio
async def test_empty_body_is_a_provider_error():
    client = _FakeClient([_empty_body()] * 3)
    service = ExtractionService("key", _FakeSessionFactory())
    service._client = client

    with pytest.raises(ExtractionError):
        await service.extract("text")


@pytest.mark.asyncio
async def test_null_completion_content_is_a_provider_error():
    client = _FakeClient([_null_content()] * 3)
    service = ExtractionService("key", _FakeSessionFactory())
    service._client = client

    with pytest.raises(ExtractionError):
        await service.extract("text")


@pytest.mark.asyncio
async def test_json_array_body_is_a_provider_error():
    """A valid JSON root that is not an object cannot describe the schema."""
    client = _FakeClient([_FakeResponse(content="[]", data=[])])
    service = ExtractionService("key", _FakeSessionFactory())
    service._client = client

    with pytest.raises(ExtractionError):
        await service.extract("text")


# ---------------- FIX-2 end-to-end: the user still gets the manual prompt ----------------
async def test_non_json_provider_answer_routes_the_user_to_manual_entry(harness):
    """The promise of ТЗ §10: no card is fine, silence is not."""
    from app.bot.states import LeadForm

    service = ExtractionService("key", harness.container.session_factory)
    await service._client.aclose()  # the real httpx client is never used here
    service._client = _FakeClient([_non_json()] * 3)
    harness.container.extraction = service

    await harness.send_text("ТОО Ромашка, Алматы, +7 700 123 45 67")
    await harness.send_command("/done")  # must not raise / must not go silent

    assert await harness.fsm_state() == LeadForm.ManualEntry.state
    assert harness.bot.contains("Не удалось распознать автоматически")
    assert harness.bot.contains("Введите название компании")
    assert await harness.lead_count() == 0


# ---------------- daily limit (429 / 402) is not «провайдер упал» ----------------
# The free OpenRouter tier allows ~50 requests/day and answers 429 once they are
# used up (402 when the balance is empty). Re-sending the request cannot help, so
# the outage chain (retry → retry → fallback → «не удалось распознать») must not
# run: the user is told *why* and the fallback is attempted at most once, and only
# when it belongs to another provider.
async def test_quota_429_tries_primary_once_then_another_provider_once():
    client = _FakeClient([_http_error(429)] * 3)
    service = ExtractionService("key", _FakeSessionFactory())
    service._client = client

    with pytest.raises(AIQuotaExceededError) as excinfo:
        await service.extract("text", session_id=7)

    assert excinfo.value.status_code == 429
    # The error carries the *last* refusal (the fallback was refused too); the log
    # has one ``ai_limit_reached`` line per model.
    assert excinfo.value.model == FALLBACK_MODEL
    # One primary attempt (no retry) + one fallback attempt (different provider).
    assert [call["model"] for call in client.calls] == [PRIMARY_MODEL, FALLBACK_MODEL]


async def test_quota_402_takes_the_same_path():
    client = _FakeClient([_http_error(402, '{"error":{"message":"insufficient credits"}}')] * 3)
    service = ExtractionService("key", _FakeSessionFactory())
    service._client = client

    with pytest.raises(AIQuotaExceededError) as excinfo:
        await service.extract("text")

    assert excinfo.value.status_code == 402
    assert [call["model"] for call in client.calls] == [PRIMARY_MODEL, FALLBACK_MODEL]


async def test_quota_does_not_retry_the_primary_model():
    """Numbers, not prose: the quota path costs exactly one call on the primary."""
    client = _FakeClient([_http_error(429)] * 3)
    # A fallback from the *same* provider would be refused for the same reason, so
    # the whole chain collapses to a single request.
    service = ExtractionService(
        "key", _FakeSessionFactory(), fallback_model="openrouter/some-other:free"
    )
    service._client = client

    with pytest.raises(AIQuotaExceededError):
        await service.extract("text")

    assert len(client.calls) == 1, "the primary model was retried despite the 429"


async def test_quota_does_not_retry_an_identical_fallback():
    client = _FakeClient([_http_error(429)] * 3)
    service = ExtractionService(
        "key", _FakeSessionFactory(), fallback_model=PRIMARY_MODEL
    )
    service._client = client

    with pytest.raises(AIQuotaExceededError):
        await service.extract("text")

    assert len(client.calls) == 1


async def test_quota_on_primary_still_lets_the_fallback_answer():
    """Degradation, not failure: a different provider may still serve the request."""
    client = _FakeClient([_http_error(429), _ok('{"company_name": "Fallback Co"}')])
    service = ExtractionService("key", _FakeSessionFactory())
    service._client = client

    result = await service.extract("text")

    assert result.company_name == "Fallback Co"
    assert [call["model"] for call in client.calls] == [PRIMARY_MODEL, FALLBACK_MODEL]


async def test_http_503_keeps_the_retry_and_fallback_chain():
    """An outage must behave exactly as before: retry, then fallback, then error."""
    client = _FakeClient([_http_error(503)] * 3)
    service = ExtractionService("key", _FakeSessionFactory())
    service._client = client

    with pytest.raises(ExtractionError) as excinfo:
        await service.extract("text")

    assert not isinstance(excinfo.value, AIQuotaExceededError)
    assert [call["model"] for call in client.calls] == [
        PRIMARY_MODEL,
        PRIMARY_MODEL,
        FALLBACK_MODEL,
    ]


async def test_quota_is_logged_as_its_own_action(caplog):
    """The limit is greppable and distinguishable from a real provider failure."""
    client = _FakeClient([_http_error(429)] * 3)
    service = ExtractionService("key", _FakeSessionFactory())
    service._client = client

    with caplog.at_level(logging.ERROR, logger="app.services.extraction"):
        with pytest.raises(AIQuotaExceededError):
            await service.extract("text", session_id=11)

    limit_records = [r for r in caplog.records if getattr(r, "action", None) == "ai_limit_reached"]
    assert limit_records, "the limit was not logged with action=ai_limit_reached"
    first = limit_records[0]
    assert first.status_code == 429
    assert first.model == PRIMARY_MODEL
    assert "free-models-per-day" in first.reason
    # A genuine outage keeps its own path — no limit action for it.
    caplog.clear()
    service._client = _FakeClient([_http_error(503)] * 3)
    with caplog.at_level(logging.ERROR, logger="app.services.extraction"):
        with pytest.raises(ExtractionError):
            await service.extract("text")
    assert not [r for r in caplog.records if getattr(r, "action", None) == "ai_limit_reached"]


async def test_quota_limit_does_not_eat_the_lead(harness):
    """The user is told about the limit, and the lead is still saveable by hand."""
    from app.bot.keyboards import CB_ADD
    from app.bot.states import LeadForm

    text = "ТОО Ромашка, Алматы, +7 700 123 45 67"
    service = ExtractionService("key", harness.container.session_factory)
    await service._client.aclose()  # the real httpx client is never used here
    service._client = _FakeClient([_http_error(429)] * 3)
    harness.container.extraction = service

    await harness.send_text(text)
    await harness.send_command("/done")

    # 1. The user gets the reason, not the generic «не удалось распознать», and the
    #    existing manual-entry scenario follows it.
    assert await harness.fsm_state() == LeadForm.ManualEntry.state
    assert harness.bot.contains("Дневной лимит бесплатных AI-запросов исчерпан")
    assert harness.bot.contains("пополнить OpenRouter")
    assert harness.bot.contains("Введите название компании")
    assert not harness.bot.contains("Не удалось распознать автоматически")
    # The message is sent with parse_mode=HTML (premium emoji), so it must survive
    # Telegram's parser: no plain emoji outside a tag, no raw angle brackets.
    limit_notice = next(
        text for text in harness.bot.texts() if "Дневной лимит" in text
    )
    assert_valid_telegram_html(limit_notice)

    # 2. Nothing was lost: no half-saved lead, the dialog is still open (not
    #    cancelled), the typed text is in the session and in raw_messages.
    assert await harness.lead_count() == 0
    sessions = await harness.sessions()
    assert len(sessions) == 1
    assert sessions[0].status == "review"
    assert text in (sessions[0].combined_text or "")
    async with harness.container.session_factory() as session:
        stored = (await session.execute(select(RawMessage))).scalars().all()
    assert any(text in (row.message_text or "") for row in stored)

    # 3. Manual entry works end to end, without any AI call.
    for answer in ("Ромашка", "+7 700 123 45 67", "Алматы", "-", "-"):
        await harness.send_text(answer)
    await harness.tap(CB_ADD)

    leads = await harness.leads()
    assert len(leads) == 1
    assert leads[0].company_name == "Ромашка"
    assert len(service._client.calls) == 2, "the limit path must not keep hammering the API"
