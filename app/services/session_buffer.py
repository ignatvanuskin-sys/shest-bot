"""Per-user session buffer: accumulates messages and finalizes on timeout or /done."""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

FinalizeCallback = Callable[[], Awaitable[None]]


class SessionBufferService:
    """Schedules a cancellable finalize timer per telegram user."""

    def __init__(self, timeout_seconds: float = 7.0):
        self.timeout_seconds = timeout_seconds
        self._timers: dict[int, asyncio.Task] = {}

    def schedule(self, user_id: int, finalize: FinalizeCallback) -> None:
        """Cancel any existing timer for the user and start a fresh one."""
        self.cancel(user_id)
        task = asyncio.create_task(self._run_timer(user_id, finalize))
        self._timers[user_id] = task

    async def _run_timer(self, user_id: int, finalize: FinalizeCallback) -> None:
        try:
            await asyncio.sleep(self.timeout_seconds)
        except asyncio.CancelledError:
            return
        self._timers.pop(user_id, None)
        try:
            await finalize()
        except Exception:
            logger.exception("finalize callback failed for user %s", user_id)

    def cancel(self, user_id: int) -> None:
        task = self._timers.pop(user_id, None)
        if task is not None and not task.done():
            task.cancel()

    def has_pending(self, user_id: int) -> bool:
        return user_id in self._timers

    async def shutdown(self) -> None:
        for task in list(self._timers.values()):
            task.cancel()
        self._timers.clear()
