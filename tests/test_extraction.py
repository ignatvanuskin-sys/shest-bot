"""Extraction tests: JSON parsing/validation + retry/fallback logic."""
from __future__ import annotations

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


class _FakeResponse:
    def __init__(self, content: str | None = None, status_code: int = 200, data: dict | None = None):
        self.status_code = status_code
        self._content = content
        self._data = data
        self.text = content or ""

    def json(self):
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
