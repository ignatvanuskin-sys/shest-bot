"""Apps Script webhook sync backend tests (httpx mocked — no live network)."""
from __future__ import annotations

import json

import httpx
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
    # Body is sent as text/plain (JSON string); the script reads e.postData.contents.
    # This only affects payload delivery — it does not avoid the /exec 302 redirect.
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


# --- Apps Script /exec redirect handling ---------------------------------------
#
# Live behaviour of the deployed script: POST /exec always answers 302 with an
# empty body and Location: https://script.googleusercontent.com/macros/echo?...;
# only the redirect target serves the JSON. Content-Type does not matter. If the
# client does not follow the redirect it parses an empty body, fails, retries,
# and writes one duplicate row per attempt.

_ECHO_URL = "https://script.googleusercontent.com/macros/echo?user_content_key=abc"


def _redirect_handler(requests: list):
    """302 on /exec (empty body) → 200 JSON on the Location target."""

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/exec":
            return httpx.Response(302, headers={"Location": _ECHO_URL}, content=b"")
        return httpx.Response(200, json={"ok": True, "row": 42})

    return handler


def _client_mirroring(svc: WebhookSheetsSyncService, handler) -> httpx.AsyncClient:
    """Mocked transport, but redirect policy/timeout copied from the service client.

    Copying `follow_redirects` pins the production setting: if it is removed from
    WebhookSheetsSyncService, this test starts receiving the empty 302 and fails.
    """
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=svc._client.follow_redirects,
        timeout=svc._client.timeout,
    )


@pytest.mark.asyncio
async def test_webhook_client_is_configured_to_follow_redirects():
    svc = WebhookSheetsSyncService("http://example.test/exec", "secret")
    try:
        assert svc._client.follow_redirects is True
    finally:
        await svc.close()


@pytest.mark.asyncio
async def test_webhook_append_follows_302_redirect_and_returns_row():
    svc = WebhookSheetsSyncService("http://example.test/exec", "secret")
    service_client = svc._client
    requests: list = []
    client = _client_mirroring(svc, _redirect_handler(requests))
    svc._client = client

    try:
        row = await svc._append(["1"] + ["x"] * 26)
    finally:
        await client.aclose()
        await service_client.aclose()

    # The row comes from the JSON served by the redirect target, not from the 302.
    assert row == 42
    assert [r.url.path for r in requests] == ["/exec", "/macros/echo"]
    assert requests[0].method == "POST"
    assert requests[0].headers["Content-Type"].startswith("text/plain")
    assert json.loads(requests[0].content.decode("utf-8"))["action"] == "append"


@pytest.mark.asyncio
async def test_webhook_without_redirect_following_fails_on_empty_302():
    """Negative control: without the fix the empty 302 makes the request fail."""
    svc = WebhookSheetsSyncService("http://example.test/exec", "secret")
    service_client = svc._client
    requests: list = []
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(_redirect_handler(requests)),
        follow_redirects=False,
    )
    svc._client = client

    try:
        with pytest.raises(sheets_mod.SheetsError) as exc:
            await svc._request("append", values=["1"] + ["x"] * 26)
    finally:
        await client.aclose()
        await service_client.aclose()

    assert "not JSON" in str(exc.value)
    # Only the first hop happened — the JSON body was never fetched.
    assert [r.url.path for r in requests] == ["/exec"]


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
