"""FIX-22 (ТЗ §11): full LLM answers are DEBUG data — capped, DB-only, and purged.

Three rules are locked down here:

* the model's answer is stored in ``extraction_logs.response_body`` for a successful
  *and* a failed attempt, truncated to ``MAX_RESPONSE_BODY_CHARS`` (the truncation is
  visible, so a reader never mistakes a cut-off body for the whole answer);
* it never reaches the stdout/file logs — a JSON log line carrying a 4 kB answer is
  unreadable and would travel off the box; only the length is logged;
* it is not kept for ever: everything older than ``EXTRACTION_LOG_RETENTION_DAYS``
  is deleted by a daily background pass, and no lead/session/audit row is touched.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app.logging_config import JsonFormatter
from app.models import AuditLog, ExtractionLog, Lead, LeadSession
from app.schemas.extraction import ExtractionResult
from app.services.background import (
    DEFAULT_RETENTION_DAYS,
    BackgroundTasks,
    ExtractionLogRetentionWorker,
    build_retention_worker,
    start_background_workers,
)
from app.services.extraction import (
    MAX_RESPONSE_BODY_CHARS,
    TRUNCATION_MARK,
    ExtractionService,
    truncate_response_body,
)
from app.services.lead_service import LeadService
from tests.integration_harness import wait_until

SECRET_MARKER = "СЕКРЕТНОЕ-ТЕЛО-ОТВЕТА"


# ---------------- fake HTTP boundary (same shape as tests/test_extraction.py) ----------------
class _FakeResponse:
    def __init__(self, content: str = "", status_code: int = 200, data: dict | None = None):
        self.status_code = status_code
        self.text = content
        self._data = data

    def json(self):
        if self._data is None:
            raise json.JSONDecodeError("Expecting value", self.text, 0)
        return self._data


class _FakeClient:
    def __init__(self, responses: list):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def post(self, url, json=None):
        self.calls.append(json)
        if not self.responses:
            return _FakeResponse("{}", data={})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def aclose(self):
        pass


def _ok(content: str) -> _FakeResponse:
    return _FakeResponse(
        content=content,
        data={
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


def _service(session_factory, *responses, log_bodies: bool = True) -> ExtractionService:
    service = ExtractionService("key", session_factory, log_bodies=log_bodies)
    service._client = _FakeClient(list(responses))
    return service


async def _logs(session_factory) -> list[ExtractionLog]:
    async with session_factory() as session:
        result = await session.execute(select(ExtractionLog).order_by(ExtractionLog.id))
        return list(result.scalars().all())


# ---------------- truncation ----------------
def test_truncate_response_body_caps_the_value_and_marks_it():
    long_value = "x" * (MAX_RESPONSE_BODY_CHARS + 500)

    stored = truncate_response_body(long_value)

    assert len(stored) == MAX_RESPONSE_BODY_CHARS, "the cap is a hard limit, marker included"
    assert stored.endswith(TRUNCATION_MARK), "a truncated body must say so"
    assert stored[:-1] == long_value[: MAX_RESPONSE_BODY_CHARS - 1]


def test_truncate_response_body_keeps_short_values_and_none():
    assert truncate_response_body("короткий ответ") == "короткий ответ"
    assert truncate_response_body("") == ""
    assert truncate_response_body(None) is None
    exact = truncate_response_body("y" * MAX_RESPONSE_BODY_CHARS)
    assert exact == "y" * MAX_RESPONSE_BODY_CHARS


# ---------------- the body reaches the database ----------------
@pytest.mark.asyncio
async def test_successful_answer_is_stored_truncated(session_factory):
    payload = json.dumps(
        {"company_name": "Ali", "description": f"{SECRET_MARKER} {'x' * MAX_RESPONSE_BODY_CHARS}"},
        ensure_ascii=False,
    )
    service = _service(session_factory, _ok(payload))

    result = await service.extract("текст", session_id=None)

    assert result.company_name == "Ali"
    logs = await _logs(session_factory)
    assert len(logs) == 1
    assert logs[0].success is True
    assert logs[0].response_body.startswith(f'{{"company_name": "Ali"')
    assert len(logs[0].response_body) == MAX_RESPONSE_BODY_CHARS
    assert logs[0].response_body.endswith(TRUNCATION_MARK)


@pytest.mark.asyncio
async def test_failed_parse_stores_the_body_next_to_the_error(session_factory):
    service = _service(session_factory, _FakeResponse("не JSON вообще"))

    with pytest.raises(Exception):
        await service.extract("текст")

    logs = await _logs(session_factory)
    assert logs and all(log.success is False for log in logs)
    assert logs[0].response_body.startswith("не JSON вообще")
    assert logs[0].error, "the failure reason must still be recorded"


@pytest.mark.asyncio
async def test_http_error_body_is_stored(session_factory):
    service = _service(session_factory, _FakeResponse(f"<html>{SECRET_MARKER}</html>", 502))

    with pytest.raises(Exception):
        await service.extract("текст")

    logs = await _logs(session_factory)
    assert logs[0].success is False
    assert SECRET_MARKER in logs[0].response_body


@pytest.mark.asyncio
async def test_network_error_stores_no_body(session_factory):
    # Every attempt fails at the transport level: there is no body to record.
    service = _service(session_factory, *([httpx.ConnectError("down")] * 3))

    with pytest.raises(Exception):
        await service.extract("текст")

    logs = await _logs(session_factory)
    assert len(logs) == 3
    assert all(log.response_body is None for log in logs), (
        "there was no response body to store"
    )


@pytest.mark.asyncio
async def test_log_llm_bodies_off_stores_nothing(session_factory):
    """The switch the owner was promised: no bodies at all, accounting still intact."""
    service = _service(session_factory, _ok('{"company_name": "Ali"}'), log_bodies=False)

    await service.extract("текст")

    logs = await _logs(session_factory)
    assert logs[0].response_body is None
    assert logs[0].success is True, "the attempt itself is still logged"


# ---------------- the body never reaches the logs ----------------
@pytest.mark.asyncio
async def test_the_body_never_appears_in_the_log_records(session_factory, caplog):
    payload = json.dumps({"company_name": SECRET_MARKER}, ensure_ascii=False)
    service = _service(session_factory, _ok(payload))
    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Collector(level=logging.DEBUG)
    target = logging.getLogger("app.services.extraction")
    target.addHandler(handler)
    previous_level = target.level
    target.setLevel(logging.DEBUG)
    try:
        await service.extract("текст")
    finally:
        target.removeHandler(handler)
        target.setLevel(previous_level)

    assert records, "the attempt must be logged"
    rendered = [
        record.getMessage() + json.dumps(record.__dict__, default=str) for record in records
    ] + [JsonFormatter().format(record) for record in records]
    assert not any(SECRET_MARKER in text for text in rendered), (
        "the response body leaked into the stdout/file logs"
    )
    # What *is* logged is the size of the body, which is what an operator needs.
    assert any(getattr(record, "response_body_chars", None) for record in records)


# ---------------- retention ----------------
async def _seed_log(session_factory, *, days_old: float, body: str | None = None) -> int:
    async with session_factory() as session:
        row = ExtractionLog(
            model="openrouter/free",
            success=True,
            response_body=body,
            created_at=datetime.now(timezone.utc) - timedelta(days=days_old),
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row.id


@pytest.mark.asyncio
async def test_purge_deletes_only_the_rows_past_the_window(session_factory):
    svc = LeadService(session_factory)
    old = await _seed_log(session_factory, days_old=31, body="старое тело")
    older = await _seed_log(session_factory, days_old=365, body="очень старое тело")
    fresh = await _seed_log(session_factory, days_old=2, body="свежее тело")

    deleted = await svc.purge_extraction_logs(30)

    assert deleted == 2
    remaining = [log.id for log in await _logs(session_factory)]
    assert remaining == [fresh], "a fresh body must survive the pass"
    assert old not in remaining and older not in remaining


@pytest.mark.asyncio
async def test_purge_keeps_everything_when_retention_is_off(session_factory):
    """``EXTRACTION_LOG_RETENTION_DAYS=0`` (or a missing value) deletes nothing."""
    svc = LeadService(session_factory)
    await _seed_log(session_factory, days_old=9999)

    assert await svc.purge_extraction_logs(0) == 0
    assert await svc.purge_extraction_logs(None) == 0
    assert len(await _logs(session_factory)) == 1


@pytest.mark.asyncio
async def test_purge_touches_nothing_but_the_extraction_logs(session_factory):
    svc = LeadService(session_factory)
    session_id = await svc.create_session(7)
    lead = await svc.add_lead(7, ExtractionResult(company_name="Ali"), session_id)
    await _seed_log(session_factory, days_old=100, body="старое тело")

    assert await svc.purge_extraction_logs(30) == 1

    assert (await svc.get_lead(lead.id)).company_name == "Ali", "a lead was deleted"
    async with session_factory() as session:
        sessions = (await session.execute(select(LeadSession))).scalars().all()
        audits = (await session.execute(select(AuditLog))).scalars().all()
        leads = (await session.execute(select(Lead))).scalars().all()
    assert len(sessions) == 1 and len(audits) >= 1 and len(leads) == 1


# ---------------- the worker ----------------
@pytest.mark.asyncio
async def test_worker_run_once_deletes_and_reports(session_factory, caplog):
    svc = LeadService(session_factory)
    await _seed_log(session_factory, days_old=45)
    await _seed_log(session_factory, days_old=1)
    worker = ExtractionLogRetentionWorker(svc, retention_days=30, interval_seconds=86400)

    with caplog.at_level(logging.INFO, logger="app.services.background"):
        deleted = await worker.run_once()

    assert deleted == 1
    records = [r for r in caplog.records if getattr(r, "action", None) == "extraction_log_cleanup"]
    assert records and records[0].deleted == 1 and records[0].retention_days == 30


@pytest.mark.asyncio
async def test_worker_loop_survives_a_failing_pass():
    passes: list[int] = []

    class FlakyWorker(ExtractionLogRetentionWorker):
        async def run_once(self) -> int:
            passes.append(len(passes) + 1)
            if len(passes) == 1:
                raise RuntimeError("database is locked")
            return 0

    worker = FlakyWorker(leads=None, retention_days=30, interval_seconds=0.01)
    task = asyncio.create_task(worker.run())
    try:
        assert await wait_until(lambda: len(passes) >= 2, timeout=3), (
            "a failing cleanup pass must not kill the worker"
        )
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_the_first_pass_runs_at_startup_not_after_a_whole_day():
    """A container redeployed daily would otherwise never reach a single pass."""
    calls: list[int] = []

    class RecordingService(_NeverDeleting):
        async def purge_extraction_logs(self, retention_days):
            calls.append(retention_days)
            return 0

    worker = ExtractionLogRetentionWorker(
        RecordingService(), retention_days=30, interval_seconds=86400
    )
    task = asyncio.create_task(worker.run())
    try:
        assert await wait_until(lambda: calls == [30], timeout=3), (
            "no cleanup ran before the first (24 h) interval"
        )
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class _NeverDeleting:
    """Stand-in for ``LeadService`` in the wiring tests (no database involved)."""

    async def purge_extraction_logs(self, retention_days):
        return 0


@pytest.mark.asyncio
async def test_worker_is_cancelled_at_shutdown():
    registry = BackgroundTasks()
    worker = ExtractionLogRetentionWorker(
        _NeverDeleting(), retention_days=30, interval_seconds=3600
    )

    task = registry.spawn_worker(worker.run(), name="extraction-log-retention")
    await asyncio.sleep(0.01)
    assert registry.pending == 1 and not task.done()

    await registry.shutdown()

    assert task.done()
    assert registry.pending == 0


# ---------------- wiring ----------------
def _settings(**overrides):
    base = {
        "DEV_POLLING": True,
        "AUTO_RESYNC_INTERVAL_SECONDS": 0,
        "GOOGLE_SHEET_ID": "",
        "EXTRACTION_LOG_RETENTION_DAYS": 30,
        "EXTRACTION_LOG_CLEANUP_INTERVAL_SECONDS": 86400,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _container(**overrides) -> SimpleNamespace:
    return SimpleNamespace(
        settings=_settings(**overrides),
        sheets=SimpleNamespace(configured=False),
        leads=_NeverDeleting(),
        tasks=BackgroundTasks(),
    )


def test_retention_defaults_to_thirty_days():
    from app.config import Settings

    assert Settings.model_fields["EXTRACTION_LOG_RETENTION_DAYS"].default == 30
    assert Settings.model_fields["LOG_LLM_BODIES"].default is True
    assert DEFAULT_RETENTION_DAYS == 30


def test_build_retention_worker_uses_the_configured_values():
    worker = build_retention_worker(_container(EXTRACTION_LOG_RETENTION_DAYS=7))

    assert worker is not None
    assert worker.retention_days == 7
    assert worker.interval_seconds == 86400.0


def test_retention_worker_is_not_started_when_disabled():
    assert build_retention_worker(_container(EXTRACTION_LOG_RETENTION_DAYS=0)) is None
    assert build_retention_worker(_container(EXTRACTION_LOG_CLEANUP_INTERVAL_SECONDS=0)) is None
    assert start_background_workers(_container(EXTRACTION_LOG_RETENTION_DAYS=0)) == []


@pytest.mark.asyncio
async def test_retention_worker_runs_without_google_sheets():
    """The extraction logs are local data — a missing sheet must not stop the purge."""
    container = _container()

    started = start_background_workers(container)

    assert len(started) == 1
    assert started[0].get_name() == "extraction-log-retention"
    await container.tasks.shutdown()
    assert container.tasks.pending == 0
