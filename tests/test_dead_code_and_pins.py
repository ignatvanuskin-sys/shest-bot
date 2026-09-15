"""FIX-18 (dead code) + FIX-20 (pinned requirements).

Two hygiene findings, both «make it impossible to come back silently»:

* ``app.database`` created a second engine on the real ``DATABASE_URL`` at *import*
  time that nothing ever disposed, plus a ``get_session`` dependency no route used;
  ``LeadService.link_raw_messages_to_lead`` and the ``phone_last_digits`` import in
  ``dedup`` had no callers at all.
* ``requirements*.txt`` listed only ranges, so two installs of the same commit could
  produce different environments.
"""
from __future__ import annotations

import re

import app.database as database
from app.services import dedup as dedup_module
from app.services.lead_service import LeadService
from app.services.normalize import phone_last_digits


def test_database_module_exposes_no_global_engine():
    """Importing app.database must not open (and leak) an engine of its own."""
    assert not hasattr(database, "engine")
    assert not hasattr(database, "SessionFactory")
    assert not hasattr(database, "get_session")
    # The builder used by the container/Alembic stays.
    assert callable(database.create_engine_and_sessionmaker)


def test_lead_service_has_no_unused_raw_message_linker():
    assert not hasattr(LeadService, "link_raw_messages_to_lead")
    # The one the save path uses is still there (it links inside the same session).
    assert hasattr(LeadService, "_link_raw_messages")


def test_dedup_does_not_import_the_unused_phone_helper():
    assert not hasattr(dedup_module, "phone_last_digits")


def test_phone_last_digits_helper_itself_is_kept():
    """It was only the *import* in dedup that was dead — the helper has users."""
    assert phone_last_digits("+77001234567", 7) == "1234567"


def _requirement_lines(path: str) -> list[str]:
    with open(path, encoding="utf-8") as handle:
        return [
            line.strip()
            for line in handle
            if line.strip() and not line.strip().startswith(("#", "-r"))
        ]


def test_requirements_are_pinned_exactly():
    for path in ("requirements.txt", "requirements-dev.txt"):
        lines = _requirement_lines(path)
        assert lines, f"{path} is empty"
        for line in lines:
            assert re.match(r"^[A-Za-z0-9_.\-]+(\[[A-Za-z0-9_,\-]+\])?==[^=<>~!]+$", line), (
                f"{path}: {line!r} is not an exact pin"
            )


def test_dev_requirements_include_the_runtime_set():
    text = open("requirements-dev.txt", encoding="utf-8").read()
    assert "-r requirements.txt" in text, "the dev set must extend the runtime set"
