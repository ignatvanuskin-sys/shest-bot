"""Startup maintenance: make the database agree with reality before serving traffic.

The dialog state (aiogram ``MemoryStorage``) and the message buffer live in memory,
so a container restart wipes them while the ``lead_sessions`` rows stay behind. Those
rows are then stuck in ``collecting``/``review`` for ever: nobody can finish a dialog
whose text is gone. The pass below closes them out at boot, so the table reflects
what actually happened instead of pretending the user is still typing.
"""
from __future__ import annotations

import logging

from app.logging_config import log_json

logger = logging.getLogger(__name__)


async def reconcile_hung_sessions(leads) -> list[int]:
    """Mark sessions interrupted by a restart as ``cancelled``; returns their ids.

    ``leads`` is a :class:`~app.services.lead_service.LeadService` (kept as a
    parameter so startup stays testable with a fake).
    """
    ids = await leads.cancel_unfinished_sessions()
    if ids:
        log_json(
            logger, 30, "sessions interrupted by a restart were cancelled on startup",
            action="sessions_reconciled", count=len(ids), session_ids=ids,
        )
    else:
        log_json(
            logger, 20, "no interrupted sessions to reconcile",
            action="sessions_reconciled", count=0,
        )
    return ids
