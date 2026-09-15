"""FIX-10: background sync jobs are tracked, drained at shutdown, and self-healing.

Two gaps in the old behaviour:

1. ``asyncio.create_task(sync_and_notify(...))`` kept no reference — the task could be
   collected mid-flight, its failure was invisible, and at shutdown the work was lost.
   Everything now goes through :class:`BackgroundTasks`, which holds a reference until
   the job finishes and drains the rest on close (``Container.close``).
2. Only a human typing ``/resync`` drained the leads whose Sheets write had failed
   (``sheet_row IS NULL``). :class:`AutoResyncWorker` does it on a timer, per the spec's
   «очередь ручной досинхронизации» safety net, and survives an unreachable Sheets API.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from app import models
from app.bot import flow
from app.config import Settings, get_settings
from app.di import Container
from app.schemas.extraction import ExtractionResult
from app.services.background import (
    AutoResyncWorker,
    BackgroundTasks,
    build_resync_worker,
    start_background_workers,
)
from app.services.lead_service import LeadService
from tests.conftest import FakeContainer
from tests.integration_harness import RecordingSheets, wait_until

# A token aiogram accepts ("<digits>:<non-empty>"); nothing is ever sent with it.
TEST_BOT_TOKEN = "123456:TEST-TOKEN-FOR-BACKGROUND-TESTS"


# ---------------- the task registry ----------------


async def test_finished_jobs_leave_the_registry():
    registry = BackgroundTasks()
    finished = asyncio.Event()

    async def job():
        finished.set()

    registry.spawn(job(), name="quick")
    assert registry.pending == 1

    await finished.wait()

    assert await wait_until(lambda: registry.pending == 0, timeout=1), (
        "a finished job must not be kept alive by the registry"
    )
    assert registry.jobs == set()


async def test_shutdown_waits_for_jobs_in_flight():
    """The whole point: work that is still running is finished, not dropped."""
    registry = BackgroundTasks()
    state: list[str] = []

    async def slow_job():
        await asyncio.sleep(0.05)
        state.append("done")

    registry.spawn(slow_job())

    await registry.shutdown()

    assert state == ["done"], "shutdown returned before the job finished"


async def test_shutdown_cancels_long_running_workers_without_hanging():
    registry = BackgroundTasks()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def worker():
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    registry.spawn_worker(worker(), name="periodic")
    await started.wait()

    await asyncio.wait_for(registry.shutdown(), timeout=1)

    assert cancelled.is_set(), "a periodic worker must be cancelled at shutdown"
    assert registry.pending == 0


async def test_shutdown_leaves_no_task_behind_after_the_timeout():
    registry = BackgroundTasks()
    state: list[str] = []

    async def stubborn():
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            state.append("cancelled")
            raise

    registry.spawn(stubborn())

    await asyncio.wait_for(registry.shutdown(timeout=0.01), timeout=2)

    assert state == ["cancelled"]
    assert registry.pending == 0


async def test_container_close_awaits_background_jobs(tmp_path, monkeypatch):
    """`Container.close()` must drain the jobs it spawned before tearing services down."""
    monkeypatch.setenv("BOT_TOKEN", TEST_BOT_TOKEN)
    monkeypatch.setenv(
        "DATABASE_URL", f"sqlite+aiosqlite:///{(tmp_path / 'close.db').as_posix()}"
    )
    get_settings.cache_clear()
    container = Container(get_settings())
    ran: list[bool] = []

    async def job():
        await asyncio.sleep(0.05)
        ran.append(True)

    container.tasks.spawn(job())

    try:
        await container.close()
    finally:
        get_settings.cache_clear()

    assert ran == [True], "a graceful shutdown must not lose the in-flight sync"


async def test_flow_spawn_sync_uses_the_registry():
    container = FakeContainer()
    lead = await container.leads.add_lead(1, ExtractionResult(company_name="Ali"))

    flow.spawn_sync(container, lead.id, 1)

    assert container.tasks.pending == 1, "the sync must be tracked, not left to the GC"
    await container.tasks.shutdown()
    assert container.tasks.pending == 0
    # The job really ran (FakeSheets cannot write → the user is told it is deferred).
    assert any("синхронизация" in text for _, text, _ in container.bot.sent)


# ---------------- the periodic resync worker ----------------


async def test_run_once_syncs_the_queue_of_every_owner(session_factory):
    leads = LeadService(session_factory)
    first = await leads.add_lead(1, ExtractionResult(company_name="Первая"))
    second = await leads.add_lead(2, ExtractionResult(company_name="Вторая"))
    sheets = RecordingSheets(row=7)

    synced = await AutoResyncWorker(leads, sheets, 300).run_once()

    assert synced == 2
    assert len(sheets.appends) == 2
    assert (await leads.get_lead(first.id)).sheet_row == 7
    assert (await leads.get_lead(second.id)).sheet_row == 7
    assert await leads.get_unsynced_leads_all_owners() == [], "the queue is drained"


async def test_run_once_is_a_noop_with_an_empty_queue(session_factory):
    sheets = RecordingSheets(row=7)

    synced = await AutoResyncWorker(LeadService(session_factory), sheets, 300).run_once()

    assert synced == 0
    assert sheets.appends == [] and sheets.synced == []


class _FixedQueue:
    """Serves a hand-picked «queue» — including rows the SQL query filters out."""

    def __init__(self, leads: list[models.Lead]):
        self.leads = leads
        self.persisted: list[tuple[int, int]] = []

    async def get_unsynced_leads_all_owners(self, limit: int = 200):
        return list(self.leads)

    async def update_lead(self, lead_id: int, **fields):
        self.persisted.append((lead_id, fields.get("sheet_row")))


async def test_run_once_never_writes_a_merged_or_deleted_row():
    """Belt and braces: even if such a row reaches the worker, is_syncable refuses it."""
    live = models.Lead(id=1, owner_user_id=1)
    dead = models.Lead(
        id=2,
        owner_user_id=1,
        duplicate_of_id=1,
        deleted_at=datetime.now(timezone.utc),
    )
    sheets = RecordingSheets(row=7)

    synced = await AutoResyncWorker(_FixedQueue([live, dead]), sheets, 300).run_once()

    assert synced == 1
    assert len(sheets.appends) == 1, "a dead row must never be appended"
    assert dead.sheet_row is None


class _ExplodingSheets(RecordingSheets):
    def __init__(self, fail_for: set[int], row: int = 7):
        super().__init__(row=row)
        self.fail_for = fail_for

    async def sync_lead(self, lead):
        if lead.id in self.fail_for:
            raise RuntimeError("sheets unreachable")
        return await super().sync_lead(lead)


async def test_run_once_survives_one_failing_lead(session_factory):
    leads = LeadService(session_factory)
    broken = await leads.add_lead(1, ExtractionResult(company_name="Первая"))
    good = await leads.add_lead(1, ExtractionResult(company_name="Вторая"))
    sheets = _ExplodingSheets(fail_for={broken.id}, row=7)

    synced = await AutoResyncWorker(leads, sheets, 300).run_once()

    assert synced == 1, "the pass must carry on after a failure"
    assert len(sheets.appends) == 1
    assert (await leads.get_lead(broken.id)).sheet_row is None
    assert (await leads.get_lead(good.id)).sheet_row == 7
    assert [lead.id for lead in await leads.get_unsynced_leads_all_owners()] == [broken.id], (
        "the failed lead stays in the queue for the next pass"
    )


async def test_run_once_logs_a_failed_lead_without_raising(session_factory, caplog):
    import logging

    leads = LeadService(session_factory)
    broken = await leads.add_lead(1, ExtractionResult(company_name="Первая"))
    sheets = _ExplodingSheets(fail_for={broken.id}, row=7)

    with caplog.at_level(logging.ERROR, logger="app.services.background"):
        synced = await AutoResyncWorker(leads, sheets, 300).run_once()

    assert synced == 0
    failures = [r for r in caplog.records if getattr(r, "action", None) == "auto_resync_lead_failed"]
    assert failures and failures[0].lead_id == broken.id


async def test_run_keeps_looping_after_a_failed_pass():
    passes: list[int] = []

    class FlakyWorker(AutoResyncWorker):
        async def run_once(self) -> int:
            passes.append(len(passes) + 1)
            if len(passes) == 1:
                raise RuntimeError("Sheets 503")
            return 0

    worker = FlakyWorker(leads=None, sheets=None, interval_seconds=0.01)
    task = asyncio.create_task(worker.run())
    try:
        assert await wait_until(lambda: len(passes) >= 2, timeout=3), (
            "a failing pass must not kill the worker"
        )
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_started_worker_is_tracked_and_stopped_on_shutdown():
    registry = BackgroundTasks()

    class EmptyQueue:
        async def get_unsynced_leads_all_owners(self, limit: int = 200):
            return []

        async def update_lead(self, lead_id: int, **fields):
            return None

    worker = AutoResyncWorker(EmptyQueue(), RecordingSheets(row=7), interval_seconds=0.01)
    task = registry.spawn_worker(worker.run(), name="auto-resync")
    await asyncio.sleep(0.05)

    assert registry.pending == 1 and not task.done()

    await registry.shutdown()

    assert task.cancelled() or task.done()
    assert registry.pending == 0


# ---------------- wiring / configuration ----------------


def _flags(**overrides) -> SimpleNamespace:
    base = {"DEV_POLLING": True, "AUTO_RESYNC_INTERVAL_SECONDS": 0.01, "GOOGLE_SHEET_ID": ""}
    base.update(overrides)
    return SimpleNamespace(**base)


def _stub_container(*, interval: float, configured: bool, leads=None) -> SimpleNamespace:
    class EmptyQueue:
        async def get_unsynced_leads_all_owners(self, limit: int = 200):
            return []

        async def update_lead(self, lead_id: int, **fields):
            return None

    return SimpleNamespace(
        settings=_flags(AUTO_RESYNC_INTERVAL_SECONDS=interval),
        sheets=SimpleNamespace(configured=configured),
        leads=leads or EmptyQueue(),
        tasks=BackgroundTasks(),
    )


async def test_start_background_workers_spawns_the_resync_worker():
    container = _stub_container(interval=0.01, configured=True)

    with_workers = start_background_workers(container)

    assert len(with_workers) == 1
    assert container.tasks.pending == 1
    await container.tasks.shutdown()
    assert container.tasks.pending == 0


async def test_worker_is_not_started_when_sheets_are_unconfigured():
    container = _stub_container(interval=0.01, configured=False)

    assert start_background_workers(container) == []
    assert container.tasks.pending == 0


async def test_interval_zero_disables_the_worker():
    container = _stub_container(interval=0, configured=True)

    assert build_resync_worker(container) is None
    assert start_background_workers(container) == []
    assert container.tasks.pending == 0


def test_auto_resync_interval_default_is_five_minutes():
    """The env knob must default to the documented ~5 minutes (300 s)."""
    assert Settings.model_fields["AUTO_RESYNC_INTERVAL_SECONDS"].default == 300.0


async def test_build_resync_worker_uses_the_configured_interval():
    container = _stub_container(interval=42, configured=True)

    worker = build_resync_worker(container)

    assert worker is not None
    assert worker.interval_seconds == 42.0
