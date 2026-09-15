"""Railway deployment wiring: PORT resolution and automatic webhook registration.

Everything here is offline: Telegram calls are replaced by fakes and migrations
are stubbed, so the suite never touches the network or the real database.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
import uvicorn

from app import main as main_mod
from app.main import DEFAULT_PORT, resolve_port, resolve_webhook_url, startup_runtime


def make_container(**settings_overrides) -> SimpleNamespace:
    """Container stub for the startup tests.

    ``startup_runtime`` also reconciles sessions the previous process left behind
    (FIX-9), so it needs ``container.leads``; a real ``Container`` would build a
    Telegram ``Bot`` and a database engine these offline tests do not need.
    """
    from tests.conftest import FakeLeads

    return SimpleNamespace(settings=make_settings(**settings_overrides), leads=FakeLeads())


def make_settings(**overrides) -> SimpleNamespace:
    base = {
        "DEV_POLLING": False,
        "WEBHOOK_URL": "",
        "WEBHOOK_SECRET": "",
        "RAILWAY_PUBLIC_DOMAIN": "",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def logged_messages():
    """Collect app.main log records.

    ``app.logging_config.setup_logging()`` swaps ``root.handlers`` wholesale, which
    detaches pytest's own caplog handler; attaching to the module logger directly
    keeps these assertions independent of global logging state.
    """
    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Collector(level=logging.DEBUG)
    target = main_mod.logger
    previous_level, previous_propagate = target.level, target.propagate
    target.addHandler(handler)
    target.setLevel(logging.DEBUG)
    target.propagate = False
    try:
        yield records
    finally:
        target.removeHandler(handler)
        target.setLevel(previous_level)
        target.propagate = previous_propagate


def messages(records) -> str:
    return "\n".join(r.getMessage() for r in records)


# --- PORT -------------------------------------------------------------------


def test_resolve_port_reads_env(monkeypatch):
    monkeypatch.setenv("PORT", "12345")
    assert resolve_port() == 12345


def test_resolve_port_defaults_when_unset(monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    assert resolve_port() == DEFAULT_PORT == 8080


def test_resolve_port_defaults_when_blank(monkeypatch):
    monkeypatch.setenv("PORT", "   ")
    assert resolve_port() == DEFAULT_PORT


def test_resolve_port_falls_back_on_garbage(monkeypatch):
    monkeypatch.setenv("PORT", "not-a-port")
    assert resolve_port() == DEFAULT_PORT


def test_main_passes_env_port_to_uvicorn(monkeypatch):
    """main() must not hardcode 8080 — Railway's $PORT has to be honoured."""
    monkeypatch.setenv("PORT", "7777")
    # Keep the test hermetic: no polling, no aiogram client, no .env dependency.
    monkeypatch.setattr(main_mod, "get_settings", lambda: make_settings(DEV_POLLING=False))
    calls: dict[str, object] = {}

    def fake_run(app_path, **kwargs):
        calls["app_path"] = app_path
        calls.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)

    main_mod.main()

    assert calls["app_path"] == "app.main:app"
    assert calls["host"] == "0.0.0.0"
    assert calls["port"] == 7777


# --- webhook URL resolution -------------------------------------------------


def test_resolve_webhook_url_prefers_explicit_url():
    settings = make_settings(
        WEBHOOK_URL="https://bot.example.com/webhook",
        RAILWAY_PUBLIC_DOMAIN="leadforge.up.railway.app",
    )
    assert resolve_webhook_url(settings) == ("https://bot.example.com/webhook", "WEBHOOK_URL")


def test_resolve_webhook_url_builds_from_railway_domain():
    settings = make_settings(RAILWAY_PUBLIC_DOMAIN="leadforge.up.railway.app")
    assert resolve_webhook_url(settings) == (
        "https://leadforge.up.railway.app/webhook",
        "RAILWAY_PUBLIC_DOMAIN",
    )


def test_resolve_webhook_url_tolerates_scheme_and_trailing_slash():
    settings = make_settings(RAILWAY_PUBLIC_DOMAIN="https://leadforge.up.railway.app/")
    assert resolve_webhook_url(settings) == (
        "https://leadforge.up.railway.app/webhook",
        "RAILWAY_PUBLIC_DOMAIN",
    )


def test_resolve_webhook_url_returns_none_when_unconfigured():
    assert resolve_webhook_url(make_settings()) is None


# --- startup wiring ---------------------------------------------------------


@pytest.fixture
def stub_startup(monkeypatch):
    """Stub migrations and capture webhook registrations instead of calling Telegram."""
    registered: list[tuple[str, str]] = []

    async def fake_migrations():
        return None

    async def fake_set_webhook(container, url, secret=None):
        registered.append((url, secret))
        return True

    monkeypatch.setattr(main_mod, "run_migrations_async", fake_migrations)
    monkeypatch.setattr(main_mod, "set_webhook", fake_set_webhook)
    return registered


async def test_startup_registers_webhook_from_railway_domain(stub_startup):
    container = make_container(RAILWAY_PUBLIC_DOMAIN="leadforge.up.railway.app", WEBHOOK_SECRET="s3cr3t")

    await startup_runtime(container)

    assert stub_startup == [("https://leadforge.up.railway.app/webhook", "s3cr3t")]


async def test_startup_prefers_explicit_webhook_url(stub_startup):
    container = make_container(
        WEBHOOK_URL="https://bot.example.com/webhook",
        RAILWAY_PUBLIC_DOMAIN="leadforge.up.railway.app",
    )

    await startup_runtime(container)

    assert stub_startup == [("https://bot.example.com/webhook", "")]


async def test_startup_logs_registration_result(stub_startup, logged_messages):
    container = make_container(RAILWAY_PUBLIC_DOMAIN="leadforge.up.railway.app")

    await startup_runtime(container)

    text = messages(logged_messages)
    assert "registering webhook at https://leadforge.up.railway.app/webhook" in text
    assert "source: RAILWAY_PUBLIC_DOMAIN" in text
    assert "webhook registration OK" in text


async def test_startup_skips_registration_without_any_url(stub_startup, logged_messages):
    container = make_container()

    await startup_runtime(container)

    assert stub_startup == []
    assert "neither WEBHOOK_URL nor RAILWAY_PUBLIC_DOMAIN" in messages(logged_messages)


async def test_startup_skips_registration_in_polling_mode(stub_startup):
    container = make_container(DEV_POLLING=True, RAILWAY_PUBLIC_DOMAIN="leadforge.up.railway.app")

    await startup_runtime(container)

    assert stub_startup == []


async def test_startup_logs_failure_without_crashing(monkeypatch, logged_messages):
    """A failed registration must be logged, not raise: /health has to keep serving."""

    async def fake_migrations():
        return None

    async def boom(container, url, secret=None):
        raise RuntimeError("telegram said no")

    monkeypatch.setattr(main_mod, "run_migrations_async", fake_migrations)
    monkeypatch.setattr(main_mod, "set_webhook", boom)
    container = make_container(RAILWAY_PUBLIC_DOMAIN="leadforge.up.railway.app")

    await startup_runtime(container)  # must not raise

    text = messages(logged_messages)
    assert "webhook registration FAILED" in text
    assert "telegram said no" in text
