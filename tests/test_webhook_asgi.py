"""The /webhook route driven through the real ASGI app — the only honest test.

Regression this module exists for (prod, silent for days): the route was declared

    async def webhook(update: Any, request: Request)

``Any`` carries no ``Body(...)`` marker, so FastAPI decided ``update`` was a
**required query parameter** and never parsed the JSON body at all. Every real
Telegram POST therefore failed validation before the first line of the handler,
the FIX-19 handler answered ``200 {"ok":true,"ignored":"unrecognised payload"}``
and nothing was ever processed. Externally everything looked healthy.

The old tests missed it because they built a ``starlette.requests.Request`` by
hand and called the route function with the payload as an argument — that skips
routing, body parsing and dependency solving entirely. These tests go through
``httpx.ASGITransport(app=app.main.app)`` instead (see
``tests.integration_harness.post_webhook``), so any wrong signature fails here.
"""
from __future__ import annotations

import json
import logging

from tests.integration_harness import (
    BASE_DATE,
    OWNER_USER_ID,
    STRANGER_USER_ID,
    harness,  # noqa: F401  — imported fixture
    no_network,  # noqa: F401  — autouse imported fixture (blocks non-loopback sockets)
    post_webhook,
)


def help_payload(user_id: int = OWNER_USER_ID, update_id: int = 900500) -> dict:
    """A real ``/help`` update, exactly the shape Telegram posts."""
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1,
            "date": BASE_DATE,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "text": "/help",
        },
    }


def body(response) -> dict:
    """JSON body of an ``httpx`` response (``TestClient``'s ``.body`` is not httpx)."""
    return response.json()


def actions(caplog) -> list[str]:
    return [getattr(record, "action", "") for record in caplog.records]


def is_ignored(response) -> bool:
    return body(response).get("ignored") == "unrecognised payload"


# ---------------- 1. a real update is really handled ----------------
async def test_a_real_post_is_parsed_and_handled(harness, caplog):
    """The regression: a JSON body in a bodyless POST must reach the dispatcher.

    ``/help`` from an allowlisted user must produce the help text. Asserting only
    on ``200`` is what let the bug live — the broken route also answered 200.
    """
    with caplog.at_level(logging.WARNING):
        response = await post_webhook(harness, json.dumps(help_payload()))

    assert response.status_code == 200
    assert body(response) == {"ok": True}, "the update must be handled, not ignored"
    assert not is_ignored(response)
    assert harness.bot.contains("Команды:"), (
        "the /help handler never ran — the body did not reach the dispatcher: "
        f"messages={harness.bot.texts()!r}"
    )
    assert harness.bot.texts_to(OWNER_USER_ID), "the reply must go to the sender"
    assert "webhook_bad_payload" not in actions(caplog)
    assert "webhook_bad_body" not in actions(caplog)


async def test_a_second_update_on_the_same_route_is_handled_too(harness):
    """Nothing about the fix may be single-shot (e.g. consuming the body once)."""
    first = await post_webhook(harness, json.dumps(help_payload(update_id=1)))
    second = await post_webhook(harness, json.dumps(help_payload(update_id=2)))

    assert [first.status_code, second.status_code] == [200, 200]
    assert body(first) == body(second) == {"ok": True}
    assert len(harness.bot.messages()) == 2


async def test_an_unknown_user_gets_the_neutral_refusal_but_is_still_handled(harness, caplog):
    """Allowlist behaviour must be unchanged: parsed, dispatched, then refused.

    The route answers 200 (the update *was* understood); it is the allowlist
    middleware that declines to run the handler and sends the neutral reply.
    """
    with caplog.at_level(logging.WARNING):
        response = await post_webhook(harness, json.dumps(help_payload(STRANGER_USER_ID)))

    assert response.status_code == 200
    assert body(response) == {"ok": True}, "a valid update is not an 'unrecognised payload'"
    assert not is_ignored(response), "the allowlist, not the parser, decides who is served"
    assert harness.bot.texts_to(STRANGER_USER_ID) == ["Это приватный бот."]
    assert not harness.bot.contains("Команды:"), "the /help handler must not run for a stranger"


# ---------------- 2. valid JSON that is not an update ----------------
async def test_json_that_is_not_an_update_is_ignored_with_200(harness, caplog):
    with caplog.at_level(logging.WARNING):
        response = await post_webhook(harness, json.dumps({"not": "an update"}))

    assert response.status_code == 200, "Telegram would replay a 5xx for ever"
    assert is_ignored(response)
    assert "webhook_bad_payload" in actions(caplog)
    assert harness.bot.messages() == [], "nothing may be processed"
    assert harness.extraction.calls == []
    assert await harness.lead_count() == 0


async def test_a_json_list_is_ignored_with_200(harness, caplog):
    with caplog.at_level(logging.WARNING):
        response = await post_webhook(harness, json.dumps([1, 2, 3]))

    assert response.status_code == 200
    assert is_ignored(response)
    assert "webhook_bad_payload" in actions(caplog)
    assert harness.bot.messages() == []


async def test_an_update_shaped_object_with_bad_types_is_ignored(harness, caplog):
    """Right keys, wrong types — pydantic refuses it, and it is still dropped."""
    payload = {"update_id": "not-an-int", "message": "nope"}

    with caplog.at_level(logging.WARNING):
        response = await post_webhook(harness, json.dumps(payload))

    assert response.status_code == 200
    assert is_ignored(response)
    assert "webhook_bad_payload" in actions(caplog)
    assert harness.bot.messages() == []


# ---------------- 3. a body that is not JSON at all ----------------
async def test_a_body_that_is_not_json_is_answered_with_200(harness, caplog):
    with caplog.at_level(logging.WARNING):
        response = await post_webhook(harness, b"<html>not json at all</html>")

    assert response.status_code == 200, "a body Telegram will resend unchanged must not 5xx"
    assert is_ignored(response)
    assert "webhook_bad_body" in actions(caplog)
    assert harness.bot.messages() == []


async def test_an_empty_body_is_answered_with_200(harness, caplog):
    with caplog.at_level(logging.WARNING):
        response = await post_webhook(harness, b"")

    assert response.status_code == 200
    assert is_ignored(response)
    assert "webhook_bad_body" in actions(caplog)


async def test_a_body_that_is_not_utf8_is_answered_with_200(harness, caplog):
    with caplog.at_level(logging.WARNING):
        response = await post_webhook(harness, b'{"update_id": 1, "x": "\xff\xfe"}')

    assert response.status_code == 200
    assert is_ignored(response)
    assert "webhook_bad_body" in actions(caplog)


async def test_a_bare_json_scalar_is_answered_with_200(harness, caplog):
    with caplog.at_level(logging.WARNING):
        response = await post_webhook(harness, b'"garbage"')

    assert response.status_code == 200
    assert is_ignored(response)
    assert "webhook_bad_payload" in actions(caplog)


# ---------------- 4. the secret header ----------------
async def test_no_secret_header_is_403_when_a_secret_is_configured(harness, monkeypatch, caplog):
    monkeypatch.setattr(harness.container.settings, "WEBHOOK_SECRET", "s3cr3t")

    with caplog.at_level(logging.WARNING):
        response = await post_webhook(harness, json.dumps(help_payload()), secret=None)

    assert response.status_code == 403
    assert harness.bot.messages() == [], "an unauthenticated update must never be dispatched"


async def test_a_wrong_secret_header_is_403(harness, monkeypatch):
    monkeypatch.setattr(harness.container.settings, "WEBHOOK_SECRET", "s3cr3t")

    response = await post_webhook(harness, json.dumps(help_payload()), secret="wrong")

    assert response.status_code == 403
    assert harness.bot.messages() == []


async def test_the_correct_secret_header_is_200_and_handled(harness, monkeypatch):
    monkeypatch.setattr(harness.container.settings, "WEBHOOK_SECRET", "s3cr3t")

    response = await post_webhook(harness, json.dumps(help_payload()), secret="s3cr3t")

    assert response.status_code == 200
    assert body(response) == {"ok": True}
    assert harness.bot.contains("Команды:")


async def test_no_secret_configured_means_no_header_is_required(harness, monkeypatch):
    monkeypatch.setattr(harness.container.settings, "WEBHOOK_SECRET", "")

    response = await post_webhook(harness, json.dumps(help_payload()), secret=None)

    assert response.status_code == 200
    assert harness.bot.contains("Команды:")


async def test_a_bad_secret_wins_over_an_unparsable_body(harness, monkeypatch):
    """Auth is checked first: garbage from an unauthenticated caller stays 403."""
    monkeypatch.setattr(harness.container.settings, "WEBHOOK_SECRET", "s3cr3t")

    response = await post_webhook(harness, b"not json", secret="wrong")

    assert response.status_code == 403


# ---------------- 5. the root cause itself ----------------
async def test_the_route_accepts_a_bodyless_post_without_a_query_string(harness):
    """A bodyless POST with no query string must work — Telegram sends exactly that.

    Under the broken signature FastAPI demanded ``update`` as a required *query*
    parameter, so this request was the one that always failed.
    """
    response = await post_webhook(harness, json.dumps(help_payload()))

    assert response.status_code == 200
    assert body(response) == {"ok": True}
    assert harness.bot.contains("Команды:")


async def test_the_openapi_schema_declares_no_webhook_parameters():
    """Schema-level guard on the root cause: no query/header params on POST /webhook.

    This is what would have caught the regression immediately — the annotation
    ``update: Any`` showed up in the generated schema as
    ``{"name": "update", "in": "query", "required": true}``.
    """
    import app.main as main_module

    operation = main_module.app.openapi()["paths"][main_module.WEBHOOK_PATH]["post"]
    parameters = operation.get("parameters", [])

    offenders = [p for p in parameters if p.get("in") in ("query", "header", "path")]
    assert not offenders, (
        "the webhook route must take its input from the JSON body only; these "
        f"parameters mean FastAPI is parsing arguments itself: {offenders!r}"
    )
    request_body = operation.get("requestBody")
    if request_body is not None:  # pragma: no cover - only if a body model is added
        assert not request_body.get("required"), "the route must accept any body shape"


async def test_a_query_string_cannot_replace_the_body(harness):
    """An unknown query string is ignored — it must not shadow the JSON body."""
    response = await post_webhook(
        harness, json.dumps(help_payload()), query="?update=garbage&foo=1"
    )

    assert response.status_code == 200
    assert body(response) == {"ok": True}
    assert harness.bot.contains("Команды:")


# ---------------- /health and the other routes stay untouched ----------------
async def test_health_is_unaffected(harness):
    from tests.integration_harness import asgi_client

    async with asgi_client(harness) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_health_still_answers_without_a_container():
    """``/health`` must never depend on the webhook container (Railway probes it)."""
    import app.main as main_module
    from tests.integration_harness import asgi_client

    async with asgi_client(None) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert main_module.app is not None
