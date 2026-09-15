"""FIX-19: an unrecognisable /webhook payload must not answer 5xx.

Telegram retries any non-2xx answer, so a body that can never be parsed was
redelivered for ever (and each attempt raised a 500 into the logs). The contract
now: 200 + a log line for a body that is not a Telegram update, 403 for a wrong
secret, 503 while the container is not initialised.

Everything here calls the real ``app.main.webhook`` route with the harness
container wired in — the same path Telegram hits.
"""
from __future__ import annotations

import json
import logging

import pytest
from fastapi import HTTPException

from tests.integration_harness import (
    BASE_DATE,
    OWNER_USER_ID,
    harness,  # noqa: F401  — imported fixture
    no_network,  # noqa: F401  — autouse imported fixture (blocks non-loopback sockets)
)


def valid_payload() -> dict:
    return {
        "update_id": 900100,
        "message": {
            "message_id": 1,
            "date": BASE_DATE,
            "chat": {"id": OWNER_USER_ID, "type": "private"},
            "from": {"id": OWNER_USER_ID, "is_bot": False, "first_name": "Test"},
            "text": "/start",
        },
    }


async def post_webhook(harness, monkeypatch, payload, secret: str | None = None):
    """POST *payload* to the real route.

    The header defaults to the container's configured secret (the local ``.env``
    may define ``WEBHOOK_SECRET``); pass ``secret=""`` to send none.
    """
    import app.main as main_module
    from starlette.requests import Request

    monkeypatch.setattr(main_module, "_container", harness.container)
    token = harness.container.settings.WEBHOOK_SECRET if secret is None else secret
    headers = []
    if token:
        headers.append((b"x-telegram-bot-api-secret-token", token.encode()))
    request = Request({"type": "http", "method": "POST", "path": "/webhook", "headers": headers})
    return await main_module.webhook(payload, request)


def body(response) -> dict:
    return json.loads(response.body)


def actions(caplog) -> list[str]:
    return [getattr(record, "action", "") for record in caplog.records]


# ---------------- unrecognisable bodies → 200 ----------------
async def test_empty_object_is_ignored_with_200(harness, monkeypatch, caplog):
    """A well-formed JSON object that is not an update (missing update_id)."""
    with caplog.at_level(logging.WARNING):
        response = await post_webhook(harness, monkeypatch, {})

    assert response.status_code == 200, "Telegram would replay a 5xx for ever"
    assert body(response)["ok"] is True
    assert body(response)["ignored"] == "unrecognised payload"
    assert "webhook_bad_payload" in actions(caplog)


async def test_list_body_is_ignored_with_200(harness, monkeypatch, caplog):
    with caplog.at_level(logging.WARNING):
        response = await post_webhook(harness, monkeypatch, [1, 2, 3])

    assert response.status_code == 200
    assert body(response) == {"ok": True, "ignored": "unrecognised payload"}
    assert "webhook_bad_payload" in actions(caplog)


async def test_string_body_is_ignored_with_200(harness, monkeypatch, caplog):
    with caplog.at_level(logging.WARNING):
        response = await post_webhook(harness, monkeypatch, "garbage")

    assert response.status_code == 200
    assert "webhook_bad_payload" in actions(caplog)


async def test_malformed_update_fields_are_ignored_with_200(harness, monkeypatch, caplog):
    """Right shape, wrong types: pydantic refuses it — the update is still dropped."""
    with caplog.at_level(logging.WARNING):
        response = await post_webhook(
            harness, monkeypatch, {"update_id": "not-an-int", "message": "nope"}
        )

    assert response.status_code == 200
    assert "webhook_bad_payload" in actions(caplog)
    assert harness.extraction.calls == [], "nothing may be processed"


async def test_the_ignored_body_is_not_dispatched(harness, monkeypatch):
    await post_webhook(harness, monkeypatch, {})
    await post_webhook(harness, monkeypatch, [])

    assert harness.bot.messages() == [], "no reply may be sent for an unknown payload"
    assert await harness.lead_count() == 0


async def test_a_body_that_is_not_json_at_all_is_answered_with_200(harness, caplog):
    """FB parsing happens inside FastAPI, so the handler is called directly here."""
    import app.main as main_module
    from fastapi.exceptions import RequestValidationError
    from starlette.requests import Request

    request = Request({"type": "http", "method": "POST", "path": "/webhook", "headers": []})
    exc = RequestValidationError(
        [{"type": "json_invalid", "loc": ("body", 0), "msg": "bad JSON", "input": None}]
    )

    with caplog.at_level(logging.WARNING):
        response = await main_module.bad_request(request, exc)

    assert response.status_code == 200
    assert body(response) == {"ok": True, "ignored": "unrecognised payload"}
    assert "webhook_bad_body" in actions(caplog)


async def test_other_routes_keep_the_normal_422():
    import app.main as main_module
    from fastapi.exceptions import RequestValidationError
    from starlette.requests import Request

    request = Request({"type": "http", "method": "POST", "path": "/health", "headers": []})
    exc = RequestValidationError(
        [{"type": "json_invalid", "loc": ("body", 0), "msg": "bad JSON", "input": None}]
    )

    response = await main_module.bad_request(request, exc)

    assert response.status_code == 422


async def test_a_real_internal_failure_is_still_500(harness, monkeypatch):
    """Only *unrecognisable input* became 200 — a broken pipeline must still shout."""
    import app.main as main_module

    async def boom(container, update):
        raise RuntimeError("database is down")

    monkeypatch.setattr(main_module, "feed_update", boom)

    with pytest.raises(HTTPException) as caught:
        await post_webhook(harness, monkeypatch, valid_payload())

    assert caught.value.status_code == 500


# ---------------- the documented statuses stay untouched ----------------
async def test_a_valid_update_is_still_processed(harness, monkeypatch):
    response = await post_webhook(harness, monkeypatch, valid_payload())

    assert response.status_code == 200
    assert body(response) == {"ok": True}
    assert harness.bot.contains("LeadForge AI")


async def test_wrong_secret_is_still_403(harness, monkeypatch):
    import app.main as main_module
    from starlette.requests import Request

    monkeypatch.setattr(main_module, "_container", harness.container)
    monkeypatch.setattr(harness.container.settings, "WEBHOOK_SECRET", "s3cr3t")
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/webhook",
            "headers": [(b"x-telegram-bot-api-secret-token", b"wrong")],
        }
    )

    try:
        await main_module.webhook(valid_payload(), request)
    except HTTPException as exc:
        assert exc.status_code == 403
    else:  # pragma: no cover - only on a regression
        raise AssertionError("a wrong secret must not be accepted")

    assert harness.bot.messages() == []


async def test_correct_secret_is_accepted(harness, monkeypatch):
    monkeypatch.setattr(harness.container.settings, "WEBHOOK_SECRET", "s3cr3t")

    response = await post_webhook(harness, monkeypatch, valid_payload(), secret="s3cr3t")

    assert response.status_code == 200
    assert harness.bot.contains("LeadForge AI")


async def test_uninitialised_container_is_still_503(harness, monkeypatch):
    import app.main as main_module
    from starlette.requests import Request

    monkeypatch.setattr(main_module, "_container", None)
    request = Request({"type": "http", "method": "POST", "path": "/webhook", "headers": []})

    try:
        await main_module.webhook(valid_payload(), request)
    except HTTPException as exc:
        assert exc.status_code == 503
    else:  # pragma: no cover - only on a regression
        raise AssertionError("an uninitialised container must answer 503")
