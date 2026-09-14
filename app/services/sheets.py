"""Google Sheets sync service — mirrors a lead row into a Google Sheet (async, retrying)."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import datetime

from app.logging_config import log_json
from app.models import Lead
from app.services.normalize import digits_only

logger = logging.getLogger(__name__)

# Column order is fixed (A..AA). See README / master prompt section 6.
COLUMN_COUNT = 27

RETRY_MAX = 5
RETRY_BASE_DELAY = 1.0

# Characters that would make Google Sheets interpret a cell as a formula.
_FORMULA_PREFIXES = ("=", "+", "-", "@")


class SheetsError(Exception):
    """Raised when a sync ultimately fails (lead stays in SQLite)."""


def escape_sheet_value(value) -> str:
    """Protect against formula injection; flatten None → empty string."""
    if value is None:
        return ""
    text = str(value)
    if text.startswith(_FORMULA_PREFIXES):
        return "'" + text
    return text


def _json_array_to_csv(value: str | None) -> str:
    if not value:
        return ""
    try:
        items = json.loads(value)
    except (TypeError, ValueError):
        return str(value)
    if isinstance(items, list):
        return ", ".join(str(item) for item in items if item is not None)
    return str(value)


def _format_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.strftime("%Y-%m-%d %H:%M:%S")


def lead_to_row_values(lead: Lead) -> list:
    """Build the 27 column values for a lead, in the fixed A..AA order."""
    return [
        escape_sheet_value(lead.id),  # A
        escape_sheet_value(_format_datetime(lead.created_at)),  # B
        escape_sheet_value(lead.company_name),  # C
        escape_sheet_value(lead.category),  # D
        escape_sheet_value(lead.city),  # E
        escape_sheet_value(lead.address),  # F
        escape_sheet_value(lead.phone),  # G
        escape_sheet_value(lead.whatsapp_number),  # H
        escape_sheet_value(lead.email),  # I
        escape_sheet_value(lead.instagram),  # J
        escape_sheet_value(lead.telegram),  # K
        escape_sheet_value(lead.website),  # L
        escape_sheet_value(lead.source),  # M
        escape_sheet_value(lead.source_url),  # N
        escape_sheet_value(_json_array_to_csv(lead.services)),  # O
        escape_sheet_value(_json_array_to_csv(lead.tags)),  # P
        escape_sheet_value(lead.description),  # Q
        escape_sheet_value(lead.rating),  # R
        escape_sheet_value(lead.reviews_count),  # S
        escape_sheet_value(lead.contact_person),  # T
        escape_sheet_value(lead.status),  # U
        escape_sheet_value(lead.priority),  # V
        escape_sheet_value(lead.pain_point),  # W
        escape_sheet_value(lead.comment),  # X
        escape_sheet_value(lead.last_action),  # Y
        escape_sheet_value(_format_datetime(lead.last_contact_at)),  # Z
        escape_sheet_value("⚠️" if lead.needs_review else ""),  # AA
    ]


class SheetsSyncService:
    """Syncs leads to Google Sheets using gspread (blocking calls offloaded to threads)."""

    def __init__(self, sheet_id: str, service_account_json_b64: str):
        self.sheet_id = sheet_id
        self._credentials_b64 = service_account_json_b64
        self._lock = asyncio.Lock()
        self._client = None
        self._worksheet = None

    @property
    def configured(self) -> bool:
        return bool(self.sheet_id and self._credentials_b64)

    def _load_credentials(self) -> dict:
        if not self._credentials_b64:
            raise SheetsError("GOOGLE_SERVICE_ACCOUNT_JSON не задан")
        try:
            raw = base64.b64decode(self._credentials_b64, validate=False).decode("utf-8")
            return json.loads(raw)
        except Exception as exc:
            raise SheetsError(f"не удалось декодировать сервис-аккаунт: {exc}") from exc

    def _ensure_client(self):
        import gspread

        if self._client is not None:
            return self._client
        creds = self._load_credentials()
        self._client = gspread.service_account_from_dict(creds)
        return self._client

    def _worksheet_sync(self):
        if self._worksheet is None:
            client = self._ensure_client()
            self._worksheet = client.open_by_key(self.sheet_id).worksheet("Leads")
        return self._worksheet

    async def _append(self, values: list) -> int:
        def _run():
            ws = self._worksheet_sync()
            ws.append_row(values, value_input_option="USER_ENTERED")
            # row index == current row count (header in row 1)
            return len(ws.get_all_values())

        return await asyncio.to_thread(_run)

    async def _update(self, row: int, values: list) -> None:
        def _run():
            ws = self._worksheet_sync()
            ws.update(f"A{row}:AA{row}", [values], value_input_option="USER_ENTERED")

        await asyncio.to_thread(_run)

    async def sync_lead(self, lead: Lead) -> int | None:
        """Append or update a lead. Returns the sheet row number, or None on failure."""
        if not self.configured:
            log_json(logger, 30, "sheets sync skipped (not configured)", lead_id=lead.id)
            return None

        values = lead_to_row_values(lead)
        async with self._lock:
            last_error: Exception | None = None
            for attempt in range(1, RETRY_MAX + 1):
                try:
                    if lead.sheet_row:
                        await self._update(lead.sheet_row, values)
                    else:
                        row = await self._append(values)
                        lead.sheet_row = row
                    log_json(
                        logger, 20, "sheets sync ok",
                        lead_id=lead.id, sheet_row=lead.sheet_row, action="sheets_sync",
                    )
                    return lead.sheet_row
                except Exception as exc:  # 429/5xx/network — retry with backoff
                    last_error = exc
                    log_json(
                        logger, 30, "sheets sync retry",
                        lead_id=lead.id, action="sheets_retry", reason=str(exc),
                    )
                    if attempt < RETRY_MAX:
                        await asyncio.sleep(RETRY_BASE_DELAY * (2 ** (attempt - 1)))
            log_json(logger, 40, "sheets sync failed after retries", lead_id=lead.id)
            return None
