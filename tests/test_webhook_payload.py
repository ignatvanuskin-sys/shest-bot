"""FIX-19: an unrecognisable /webhook payload must not answer 5xx.

Telegram retries any non-2xx answer, so a body that can never be parsed was
redelivered for ever (and each attempt raised a 500 into the logs). The contract
now: 200 + a log line for a body that is not a Telegram update, 403 for a wrong
secret, 503 while the container is not initialised.

Everything here POSTs to the real ``app.main`` app over ASGI with the harness
container wired in — the same path Telegram hits. The earlier version of this
module called ``app.main.webhook(payload, request)`` with a hand-built
``Request``, which quietly skipped the request parsing FastAPI does; that is how
a route whose signature demanded ``update`` as a *query parameter* stayed green
while production ignored every update (see ``tests/test_webhook_asgi.py``).
"""
from __future__ import annotations

import json
import logging

from tests.integration_harness import (
    BASE_DATE,
    CONFIGURED_SECRET,
    OWNER_USER_ID,
    harness,  # noqa: F401  — imported fixture
    no_network,  # noqa: F401  — autouse imported fixture (blocks non-loopback sockets)
    post_webhook,
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


async def post(harness, payload, secret=CONFIGURED_SECRET):
    """POST *payload* through the real ASGI app.

    A ``dict``/``list``/``str`` is JSON-encoded (a bare Python string is still a
    well-formed JSON body: ``"garbage"``); ``bytes`` are sent verbatim, which is how
    a body that does not parse is produced.

    ``secret`` defaults to whatever the container is configured with — the local
    ``.env`` defines ``WEBHOOK_SECRET``, and the container is built from it — so the
    tests below exercise the handler rather than the auth check. Pass a string for a
    specific token, or ``None`` to send no header at all.
    """
    body = payload if isinstance(payload, bytes) else json.dumps(payload)
    return await post_webhook(harness, body, secret=secret)


def body(response) -> dict:
    return response.json()


def actions(caplog) -> list[str]:
    return [getattr(record, "action", "") for record in caplog.records]


# ---------------- unrecognisable bodies → 200 ----------------
async def test_empty_object_is_ignored_with_200(harness, caplog):
    """A well-formed JSON object that is not an update (missing update_id)."""
    with caplog.at_level(logging.WARNING):
        response = await post(harness, {})

    assert response.status_code == 200, "Telegram would replay a 5xx for ever"
    assert body(response)["ok"] is True
    assert body(response)["ignored"] == "unrecognised payload"
    assert "webhook_bad_payload" in actions(caplog)


async def test_list_body_is_ignored_with_200(harness, caplog):
    with caplog.at_level(logging.WARNING):
        response = await post(harness, [1, 2, 3])

    assert response.status_code == 200
    assert body(response) == {"ok": True, "ignored": "unrecognised payload"}
    assert "webhook_bad_payload" in actions(caplog)


async def test_string_body_is_ignored_with_200(harness, caplog):
    with caplog.at_level(logging.WARNING):
        response = await post(harness, "garbage")

    assert response.status_code == 200
    assert "webhook_bad_payload" in actions(caplog)


async def test_malformed_update_fields_are_ignored_with_200(harness, caplog):
    """Right shape, wrong types: pydantic refuses it — the update is still dropped."""
    with caplog.at_level(logging.WARNING):
        response = await post(harness, {"update_id": "not-an-int", "message": "nope"})

    assert response.status_code == 200
    assert "webhook_bad_payload" in actions(caplog)
    assert harness.extraction.calls == [], "nothing may be processed"


async def test_the_ignored_body_is_not_dispatched(harness):
    await post(harness, {})
    await post(harness, [])

    assert harness.bot.messages() == [], "no reply may be sent for an unknown payload"
    assert await harness.lead_count() == 0


async def test_a_body_that_is_not_json_at_all_is_answered_with_200(harness, caplog):
    """Raw bytes on the wire, through the whole ASGI stack — no hand-built Request."""
    with caplog.at_level(logging.WARNING):
        response = await post(harness, b"<html>definitely not json</html>")

    assert response.status_code == 200
    assert body(response) == {"ok": True, "ignored": "unrecognised payload"}
    assert "webhook_bad_body" in actions(caplog)


async def test_other_routes_keep_the_normal_422():
    """The blanket 200 is scoped to the webhook path, not applied app-wide.

    No other route parses a body any more, so this exercises the handler directly —
    it is the unit under test here, not the route behaviour.
    """
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

    response = await post(harness, valid_payload())

    assert response.status_code == 500, "a broken pipeline must not be dressed up as 'ignored'"


# ---------------- the documented statuses stay untouched ----------------
async def test_a_valid_update_is_still_processed(harness):
    response = await post(harness, valid_payload())

    assert response.status_code == 200
    assert body(response) == {"ok": True}
    assert harness.bot.contains("LeadForge AI")


async def test_wrong_secret_is_still_403(harness, monkeypatch):
    monkeypatch.setattr(harness.container.settings, "WEBHOOK_SECRET", "s3cr3t")

    response = await post(harness, valid_payload(), secret="wrong")

    assert response.status_code == 403
    assert harness.bot.messages() == []


async def test_correct_secret_is_accepted(harness, monkeypatch):
    monkeypatch.setattr(harness.container.settings, "WEBHOOK_SECRET", "s3cr3t")

    response = await post(harness, valid_payload(), secret="s3cr3t")

    assert response.status_code == 200
    assert harness.bot.contains("LeadForge AI")


async def test_uninitialised_container_is_still_503():
    """Posting with no container wired in answers 503, not a body-parse 200."""
    from tests.integration_harness import post_webhook as asgi_post

    response = await asgi_post(None, json.dumps(valid_payload()))

    assert response.status_code == 503
