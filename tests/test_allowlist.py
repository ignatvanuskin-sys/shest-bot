"""Allowlist tests: unknown user is blocked, known user passes."""
from __future__ import annotations

import pytest

from app.bot import middlewares as mw_mod
from app.bot.middlewares import AllowlistMiddleware, is_allowed
from tests.conftest import FakeContainer


def test_is_allowed():
    assert is_allowed({1, 2}, 1) is True
    assert is_allowed({1, 2}, 3) is False
    assert is_allowed(set(), 1) is False


@pytest.mark.asyncio
async def test_middleware_allows_known_user(monkeypatch):
    container = FakeContainer()  # FakeSettings.allowed_user_ids == {1}
    mw = AllowlistMiddleware(container)
    calls = []

    async def handler(event, data):
        calls.append(event)

    monkeypatch.setattr(mw_mod, "_extract_user_id", lambda e: 1)
    await mw(handler, object(), {})
    assert len(calls) == 1
    assert container.bot.sent == []


@pytest.mark.asyncio
async def test_middleware_blocks_unknown_user(monkeypatch):
    container = FakeContainer()
    mw = AllowlistMiddleware(container)
    calls = []

    async def handler(event, data):
        calls.append(event)

    monkeypatch.setattr(mw_mod, "_extract_user_id", lambda e: 999)
    await mw(handler, object(), {})
    assert calls == []
    assert any("приватный" in text for _, text, _ in container.bot.sent)


@pytest.mark.asyncio
async def test_middleware_empty_allowlist_blocks_and_warns(monkeypatch):
    container = FakeContainer()
    container.settings.allowed_user_ids = set()
    mw = AllowlistMiddleware(container)
    calls = []

    async def handler(event, data):
        calls.append(event)

    monkeypatch.setattr(mw_mod, "_extract_user_id", lambda e: 777)
    await mw(handler, object(), {})
    assert calls == []
    assert any("приватный" in text for _, text, _ in container.bot.sent)
