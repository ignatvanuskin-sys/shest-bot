"""Apps Script webhook sync backend tests (httpx mocked — no live network)."""
from __future__ import annotations

import json

import pytest

from app import models
from app.services import sheets as sheets_mod
from app.services.sheets import (
    SheetsSyncService,
    WebhookSheetsSyncService,
    build_sheets_service,
)


class _FakeWebhookResponse:
    def __init__(self, status_code: int, data: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._data = data
        self.text = text

    def json(self):
        return self._data if self._data is not None else {}


class _FakeWebhookClient:
    def __init__(self, responses: list):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def post(self, url, content=None, headers=None):
        self.calls.append({"url": url, "content": content, "headers": headers})
        return self.responses.pop(0)

    async def aclose(self):
        pass


def _body(call: dict) -> dict:
    return json.loads(call["content"].decode("utf-8"))


@pytest.mark.asyncio
async def test_webhook_append_returns_row_and_caches():
    svc = WebhookSheetsSyncService("http://example.test/exec", "secret")
    svc._client = _FakeWebhookClient([_FakeWebhookResponse(200, {"ok": True, "row": 9})])
    lead = models.Lead(id=1)

    row = await svc.sync_lead(lead)

    assert row == 9
    assert lead.sheet_row == 9
    call = svc._client.calls[0]
    # Critical detail: body is sent as text/plain (JSON string), never application/json.
    assert call["headers"]["Content-Type"].startswith("text/plain")
    body = _body(call)
    assert body["token"] == "secret"
    assert body["action"] == "append"
    assert body["row"] is None
    assert len(body["values"]) == 27


@pytest.mark.asyncio
async def test_webhook_update_uses_cached_row():
    svc = WebhookSheetsSyncService("http://example.test/exec", "secret")
    svc._client = _FakeWebhookClient([_FakeWebhookResponse(200, {"ok": True})])
    lead = models.Lead(id=1, sheet_row=7)

    row = await svc.sync_lead(lead)

    assert row == 7
    body = _body(svc._client.calls[0])
    assert body["action"] == "update"
    assert body["row"] == 7


@pytest.mark.asyncio
async def test_webhook_retries_on_500_then_succeeds(monkeypatch):
    monkeypatch.setattr(sheets_mod, "RETRY_BASE_DELAY", 0.0)
    svc = WebhookSheetsSyncService("http://example.test/exec", "secret")
    svc._client = _FakeWebhookClient(
        [
            _FakeWebhookResponse(500, text="boom"),
            _FakeWebhookResponse(200, {"ok": True, "row": 3}),
        ]
    )
    lead = models.Lead(id=1)

    row = await svc.sync_lead(lead)

    assert row == 3
    assert len(svc._client.calls) == 2


@pytest.mark.asyncio
async def test_webhook_invalid_token_raises():
    svc = WebhookSheetsSyncService("http://example.test/exec", "secret")
    svc._client = _FakeWebhookClient(
        [_FakeWebhookResponse(200, {"ok": False, "error": "invalid token"})]
    )

    with pytest.raises(sheets_mod.SheetsError) as exc:
        await svc._request("append", values=["x"] * 27)

    assert "invalid token" in str(exc.value)


def test_backend_selection_priority():
    class _ServiceAccount:
        GOOGLE_SERVICE_ACCOUNT_JSON = "e30="
        GOOGLE_SHEET_ID = "sheet-id"
        GOOGLE_SHEETS_WEBHOOK_URL = "http://example.test/exec"
        GOOGLE_SHEETS_WEBHOOK_TOKEN = "t"

    class _WebhookOnly:
        GOOGLE_SERVICE_ACCOUNT_JSON = ""
        GOOGLE_SHEET_ID = ""
        GOOGLE_SHEETS_WEBHOOK_URL = "http://example.test/exec"
        GOOGLE_SHEETS_WEBHOOK_TOKEN = "t"

    class _NoneConfigured:
        GOOGLE_SERVICE_ACCOUNT_JSON = ""
        GOOGLE_SHEET_ID = ""
        GOOGLE_SHEETS_WEBHOOK_URL = ""
        GOOGLE_SHEETS_WEBHOOK_TOKEN = ""

    # Service account wins over webhook.
    assert isinstance(build_sheets_service(_ServiceAccount()), SheetsSyncService)
    assert isinstance(build_sheets_service(_WebhookOnly()), WebhookSheetsSyncService)

    not_configured = build_sheets_service(_NoneConfigured())
    assert isinstance(not_configured, SheetsSyncService)
    assert not_configured.configured is False
