"""Background jobs: tracked fire-and-forget tasks and the periodic workers.

``asyncio.create_task`` keeps no strong reference, so a task can be garbage-collected
mid-flight, its exception is invisible unless someone awaits it, and at shutdown the
work is simply lost. Every background job in this application goes through
:class:`BackgroundTasks`: references are kept until completion, failures are logged,
and shutdown drains what is still running.

Two long-running workers live here:

* :class:`AutoResyncWorker` is the automatic half of the «очередь ручной
  досинхронизации» safety net: leads whose Sheets write failed stay in SQLite with
  ``sheet_row IS NULL``, and they used to wait for a human to type ``/resync``.
* :class:`ExtractionLogRetentionWorker` is the ТЗ §11 retention pass: full LLM
  response bodies are DEBUG data and are deleted once they are older than
  ``EXTRACTION_LOG_RETENTION_DAYS``. Nothing else about a lead is touched.
"""
from __future__ import annotations

import asyncio
import logging

from app.logging_config import log_json

logger = logging.getLogger(__name__)

# Grace period for one-shot jobs at shutdown: they hold the DB session and the
# Telegram call of the lead that was just saved.
DEFAULT_SHUTDOWN_TIMEOUT = 5.0

# Documented defaults for the retention pass, also used when a (test) container
# stub does not carry the settings attribute at all — the cleanup must never
# silently disappear from a running application.
DEFAULT_RETENTION_DAYS = 30
DEFAULT_RETENTION_INTERVAL_SECONDS = 86400.0


class BackgroundTasks:
    """Registry of tracked background tasks (one-shot jobs + long-running workers)."""

    def __init__(self) -> None:
        self._jobs: set[asyncio.Task] = set()
        self._workers: set[asyncio.Task] = set()

    def spawn(self, coro, *, name: str | None = None) -> asyncio.Task:
        """Track a one-shot job (sheet sync, notification)."""
        return self._track(coro, self._jobs, name)

    def spawn_worker(self, coro, *, name: str | None = None) -> asyncio.Task:
        """Track a long-running service loop; it is cancelled first at shutdown."""
        return self._track(coro, self._workers, name)

    def _track(self, coro, bucket: set, name: str | None) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        bucket.add(task)
        # Drop the reference as soon as the task is done — the set is a keep-alive
        # list, not a history (a done task is not "background work in flight").
        task.add_done_callback(bucket.discard)
        return task

    @property
    def jobs(self) -> set[asyncio.Task]:
        return set(self._jobs)

    @property
    def workers(self) -> set[asyncio.Task]:
        return set(self._workers)

    @property
    def pending(self) -> int:
        return len(self._jobs) + len(self._workers)

    async def shutdown(self, *, timeout: float = DEFAULT_SHUTDOWN_TIMEOUT) -> None:
        """Stop workers, then let the one-shot jobs in flight finish (bounded)."""
        workers = list(self._workers)
        for task in workers:
            task.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

        pending = list(self._jobs)
        if not pending:
            return
        _, still_running = await asyncio.wait(pending, timeout=timeout)
        for task in still_running:
            task.cancel()
        if still_running:
            await asyncio.gather(*still_running, return_exceptions=True)

    def cancel_all(self) -> None:
        """Cancel everything without waiting (used only by tests/teardown)."""
        for task in list(self._workers) + list(self._jobs):
            task.cancel()


class AutoResyncWorker:
    """Periodically mirrors the leads that never got a sheet row.

    Errors are contained: a failing lead is logged and the pass moves on, and a
    failing pass never kills the loop (the worker has to outlive an unreachable
    Sheets API).
    """

    def __init__(self, leads, sheets, interval_seconds: float = 300.0):
        self.leads = leads
        self.sheets = sheets
        self.interval_seconds = float(interval_seconds)

    async def run(self) -> None:
        """Loop forever: wait one interval, then drain the queue."""
        log_json(
            logger, 20, "auto-resync worker started",
            action="auto_resync_started", interval_seconds=self.interval_seconds,
        )
        while True:
            await asyncio.sleep(self.interval_seconds)
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A broken pass must not kill the safety net.
                logger.exception("auto-resync pass failed")

    async def run_once(self) -> int:
        """One pass over the queue. Returns how many leads were written."""
        queued = await self.leads.get_unsynced_leads_all_owners()
        synced = 0
        for lead in queued:
            try:
                # ``sync_lead`` re-applies is_syncable: merged/deleted rows are
                # refused even if they somehow reach this list.
                row = await self.sheets.sync_lead(lead)
                if row:
                    await self.leads.update_lead(lead.id, sheet_row=row)
                    synced += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_json(
                    logger, 40, "auto-resync failed for one lead",
                    action="auto_resync_lead_failed", lead_id=lead.id, reason=str(exc),
                )
        log_json(
            logger, 20 if synced else 30, "auto-resync pass finished",
            action="auto_resync", queued=len(queued), synced=synced,
        )
        return synced


class ExtractionLogRetentionWorker:
    """Periodically deletes ``extraction_logs`` rows past their retention window.

    ТЗ §11 allows the full LLM bodies to be kept for debugging only: they are stored
    at DEBUG level (``LOG_LLM_BODIES``) and dropped after
    ``EXTRACTION_LOG_RETENTION_DAYS``. The pass is idempotent, bounded by the same
    discipline as :class:`AutoResyncWorker` (a failing pass is logged and the loop
    survives it — cleanup must never take the bot down), and it deletes *only*
    extraction logs: leads, sessions and the audit trail are untouched.
    """

    def __init__(self, leads, retention_days: int, interval_seconds: float = 86400.0):
        self.leads = leads
        self.retention_days = int(retention_days)
        self.interval_seconds = float(interval_seconds)

    async def run(self) -> None:
        """One pass at startup, then one per interval.

        The first pass runs *before* the first sleep on purpose: the interval is a
        day long and a container is redeployed far more often than that (Railway
        restarts on every push), so «wait 24 h, then purge» would never actually
        purge anything. The pass is a bounded, idempotent DELETE.
        """
        log_json(
            logger, 20, "extraction-log retention worker started",
            action="extraction_log_cleanup_started",
            retention_days=self.retention_days, interval_seconds=self.interval_seconds,
        )
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A broken pass (locked DB, missing table) must not kill the worker.
                logger.exception("extraction-log retention pass failed")
            await asyncio.sleep(self.interval_seconds)

    async def run_once(self) -> int:
        """One pass. Returns how many log rows were deleted."""
        deleted = await self.leads.purge_extraction_logs(self.retention_days)
        log_json(
            logger, 20 if not deleted else 30, "extraction-log retention pass finished",
            action="extraction_log_cleanup", retention_days=self.retention_days,
            deleted=deleted,
        )
        return deleted


def build_resync_worker(container) -> AutoResyncWorker | None:
    """Auto-resync worker for this configuration, or None when it should not run."""
    interval = float(getattr(container.settings, "AUTO_RESYNC_INTERVAL_SECONDS", 0.0) or 0.0)
    if interval <= 0:
        log_json(logger, 20, "auto-resync disabled (interval <= 0)", action="auto_resync_off")
        return None
    sheets = getattr(container, "sheets", None)
    if sheets is None or not getattr(sheets, "configured", False):
        log_json(
            logger, 20, "auto-resync skipped (Google Sheets not configured)",
            action="auto_resync_skipped",
        )
        return None
    return AutoResyncWorker(container.leads, sheets, interval)


def build_retention_worker(container) -> ExtractionLogRetentionWorker | None:
    """Retention worker for this configuration, or None when it should not run.

    Independent of Google Sheets: the extraction logs live in the local database
    whether or not the table is configured. ``EXTRACTION_LOG_RETENTION_DAYS <= 0``
    (or an interval <= 0) switches it off.
    """
    settings = getattr(container, "settings", None)
    # 0 means "off", a *missing* value means "use the documented default" — hence the
    # explicit read (``getattr(...) or DEFAULT`` would turn 0 back into 30 days).
    try:
        retention_days = int(
            getattr(settings, "EXTRACTION_LOG_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)
        )
    except (TypeError, ValueError):
        retention_days = DEFAULT_RETENTION_DAYS
    if retention_days <= 0:
        log_json(
            logger, 20, "extraction-log cleanup disabled (retention <= 0 days)",
            action="extraction_log_cleanup_off",
        )
        return None
    try:
        interval = float(
            getattr(
                settings,
                "EXTRACTION_LOG_CLEANUP_INTERVAL_SECONDS",
                DEFAULT_RETENTION_INTERVAL_SECONDS,
            )
        )
    except (TypeError, ValueError):
        interval = DEFAULT_RETENTION_INTERVAL_SECONDS
    if interval <= 0:
        log_json(
            logger, 20, "extraction-log cleanup disabled (interval <= 0)",
            action="extraction_log_cleanup_off",
        )
        return None
    leads = getattr(container, "leads", None)
    if leads is None or not hasattr(leads, "purge_extraction_logs"):
        logger.warning("container has no lead service — extraction-log cleanup not started")
        return None
    return ExtractionLogRetentionWorker(leads, retention_days, interval)


def start_background_workers(container) -> list[asyncio.Task]:
    """Start every long-running worker for this configuration."""
    started: list[asyncio.Task] = []
    tasks = getattr(container, "tasks", None)
    if tasks is None:
        logger.warning("container has no task registry — no background worker started")
        return started

    worker = build_resync_worker(container)
    if worker is not None:
        started.append(tasks.spawn_worker(worker.run(), name="auto-resync"))
        log_json(
            logger, 20, "auto-resync worker scheduled",
            action="auto_resync_scheduled", interval_seconds=worker.interval_seconds,
        )

    retention = build_retention_worker(container)
    if retention is not None:
        started.append(tasks.spawn_worker(retention.run(), name="extraction-log-retention"))
        log_json(
            logger, 20, "extraction-log retention worker scheduled",
            action="extraction_log_cleanup_scheduled",
            retention_days=retention.retention_days,
            interval_seconds=retention.interval_seconds,
        )
    return started
