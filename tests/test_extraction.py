"""Extraction tests: JSON parsing/validation + retry/fallback logic."""
from __future__ import annotations

import json

import httpx
import pytest

from app.schemas.extraction import ExtractionResult
from app.services.extraction import (
    FALLBACK_MODEL,
    PRIMARY_MODEL,
    ExtractionError,
    ExtractionService,
    _InvalidJsonError,
    parse_extraction_content,
)
from tests.integration_harness import (
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
