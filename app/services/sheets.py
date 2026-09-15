"""Google Sheets sync service — mirrors a lead row into a Google Sheet (async, retrying).

Two rules keep the table and the database from drifting apart (FIX-7/FIX-8):

* a row is written only after its column A is confirmed to hold this lead's ID — a
  cached ``sheet_row`` is a hint, never proof (a hand-edited table shifts rows);
* appending is idempotent by ID, so a retry after a lost response reuses the existing
  line instead of adding a second one.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import datetime, timedelta, timezone, tzinfo
from functools import lru_cache

import httpx

from app.logging_config import log_json
from app.models import Lead

logger = logging.getLogger(__name__)

# Column order is fixed (A..AA). See README / master prompt section 6.
COLUMN_COUNT = 27

RETRY_MAX = 5
RETRY_BASE_DELAY = 1.0

# Characters that would make Google Sheets interpret a cell as a formula.
_FORMULA_PREFIXES = ("=", "+", "-", "@")

# Zones the table can be shown in when the IANA database is not installed (Windows
# ships none, and the ``tzdata`` package is optional). The offset is what matters and
# these zones have had no DST for years, so a fixed offset is exact for them.
_FALLBACK_UTC_OFFSETS: dict[str, float] = {
    "asia/almaty": 5.0,
    "asia/astana": 5.0,
    "asia/aqtobe": 5.0,
    "asia/atyrau": 5.0,
    "asia/qyzylorda": 5.0,
    "asia/oral": 5.0,
    "asia/tashkent": 5.0,
    "asia/bishkek": 6.0,
    "europe/moscow": 3.0,
    "asia/yekaterinburg": 5.0,
    "asia/novosibirsk": 7.0,
    "utc": 0.0,
}
DEFAULT_DISPLAY_TIMEZONE = "Asia/Almaty"


@lru_cache(maxsize=32)
def display_timezone(name: str | None) -> tzinfo:
    """Resolve a timezone name for *display* purposes; never raises.

    Falls back to a fixed UTC offset (then to UTC with a warning) when the IANA
    database is unavailable, so a missing ``tzdata`` package can never make the
    sheet write fail. An explicit ``+05:00``/``-03:00`` style value also works.
    """
    raw = (name or "").strip() or DEFAULT_DISPLAY_TIMEZONE
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(raw)
    except Exception:
        pass
    key = raw.lower()
    if key in _FALLBACK_UTC_OFFSETS:
        return timezone(timedelta(hours=_FALLBACK_UTC_OFFSETS[key]))
    digits = raw.replace("UTC", "").replace("utc", "").strip()
    try:  # "+05:00", "5", "-3"
        sign = -1.0 if digits.startswith("-") else 1.0
        parts = digits.lstrip("+-").split(":")
        hours = float(parts[0])
        minutes = float(parts[1]) if len(parts) > 1 else 0.0
        return timezone(sign * timedelta(hours=hours, minutes=minutes))
    except (ValueError, IndexError):
        logger.warning(
            "unknown DISPLAY_TIMEZONE %r (no tz database?) — falling back to UTC", raw
        )
        return timezone.utc


def get_display_timezone() -> tzinfo:
    """The configured display timezone (``DISPLAY_TIMEZONE``, default Asia/Almaty)."""
    from app.config import get_settings

    return display_timezone(getattr(get_settings(), "DISPLAY_TIMEZONE", None))


class SheetsError(Exception):
    """Raised when a sync ultimately fails (lead stays in SQLite)."""


def is_syncable(lead: Lead) -> bool:
    """Whether *lead* may be mirrored into the sheet at all.

    A merge (``LeadService.add_lead(merge_target_id=...)``) keeps the incoming data
    as a row marked ``duplicate_of_id`` + ``deleted_at``. That row never had a
    ``sheet_row``, so mirroring it *appends* a second line for a company already in
    the table. Only live rows own a sheet line.
    """
    return lead.deleted_at is None and lead.duplicate_of_id is None


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


def _format_datetime(value: datetime | None, tz: tzinfo | None = None) -> str:
    """Render a stored (UTC) timestamp in the display timezone (FIX-14).

    The database keeps UTC; without the shift the owner in UTC+5 saw «Дата
    добавления» five hours in the past. A naive value is treated as UTC — that is
    how both ``CURRENT_TIMESTAMP`` and the app's ``datetime.now(timezone.utc)``
    writes reach SQLite.
    """
    if value is None:
        return ""
    moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    try:
        moment = moment.astimezone(tz or get_display_timezone())
    except (OverflowError, ValueError):  # pragma: no cover - only for absurd inputs
        return moment.strftime("%Y-%m-%d %H:%M:%S")
    return moment.strftime("%Y-%m-%d %H:%M:%S")


# ---------------- row identity (FIX-7 / FIX-8) ----------------
#
# ``lead.sheet_row`` is only a *cache* of a row number. It goes stale whenever the
# table is edited by hand: inserting or deleting one line shifts every line below it,
# and the bot would silently overwrite whatever lead ended up on the cached number.
# Both backends therefore verify column A before writing — the exact same rule the
# Apps Script webhook implements in ``findRowById`` (docs/google_apps_script_webhook.gs).


def lead_id_from_values(values: list) -> str:
    """The lead ID of a row payload. Column A by contract (shared with the .gs script)."""
    if not values:
        return ""
    first = values[0]
    return "" if first is None else str(first).strip()


def row_holds_lead(column_a: list, row: int, lead_id) -> bool:
    """Whether sheet ``row`` (1-based) carries *lead_id* in column A."""
    if row is None or row < 1 or row > len(column_a):
        return False
    value = column_a[row - 1]
    return value is not None and str(value).strip() == str(lead_id)


def find_row_by_id(column_a: list, lead_id) -> int | None:
    """Row (1-based) whose column A is exactly *lead_id*; row 1 (headers) is skipped."""
    target = "" if lead_id is None else str(lead_id).strip()
    if not target:
        return None
    for index, value in enumerate(column_a, start=1):
        if index == 1 or value is None:
            continue
        if str(value).strip() == target:
            return index
    return None


def resolve_row_for_lead(column_a: list, row: int, lead_id) -> int:
    """The row that actually holds *lead_id* — never a bare cached number.

    Raises :class:`SheetsError` when the ID is nowhere in column A: writing to the
    cached row anyway is exactly the corruption this guards against.
    """
    if row_holds_lead(column_a, row, lead_id):
        return row
    found = find_row_by_id(column_a, lead_id)
    if found is None:
        raise SheetsError(
            f"строка {row} не принадлежит лиду {lead_id}, и лид не найден в колонке A"
        )
    log_json(
        logger, 30, "sheet row moved — writing to the row that holds this lead",
        lead_id=lead_id, action="sheets_row_moved", cached_row=row, sheet_row=found,
    )
    return found


def lead_to_row_values(lead: Lead, tz: tzinfo | None = None) -> list:
    """Build the 27 column values for a lead, in the fixed A..AA order.

    *tz* overrides the display timezone of the two date columns (tests); by default
    ``DISPLAY_TIMEZONE`` is used.
    """
    return [
        escape_sheet_value(lead.id),  # A
        escape_sheet_value(_format_datetime(lead.created_at, tz)),  # B
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
        escape_sheet_value(_format_datetime(lead.last_contact_at, tz)),  # Z
        escape_sheet_value("⚠️" if lead.needs_review else ""),  # AA
    ]


class BaseSheetsSyncService:
    """Shared retry/lock semantics for both sync backends (gspread + webhook)."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return False

    async def sync_lead(self, lead: Lead) -> int | None:
        """Append or update a lead. Returns the sheet row number, or None on failure."""
        if not is_syncable(lead):
            # Merged/deleted rows are audit history only. Syncing one would append a
            # duplicate line (no sheet_row) or overwrite a live row with stale data.
            log_json(
                logger, 30, "sheets sync refused (lead is merged or deleted)",
                lead_id=lead.id, action="sheets_sync_refused",
                duplicate_of_id=lead.duplicate_of_id,
                deleted=lead.deleted_at is not None,
            )
            return None

        if not self.configured:
            log_json(logger, 30, "sheets sync skipped (not configured)", lead_id=lead.id)
            return None

        values = lead_to_row_values(lead)
        async with self._lock:
            last_error: Exception | None = None
            for attempt in range(1, RETRY_MAX + 1):
                try:
                    if lead.sheet_row:
                        # ``lead_id`` makes the backend verify that the cached row is
                        # really this lead's line (the table may have been edited by
                        # hand). A backend that re-targeted the row reports it back so
                        # the cache can be repaired instead of drifting for ever.
                        actual = await self._update(
                            lead.sheet_row, values, lead_id=lead.id
                        )
                        if actual and actual != lead.sheet_row:
                            lead.sheet_row = actual
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

    async def clear_row(self, row: int | None, lead_id: int | None = None) -> bool:
        """Blank the 27 cells of a sheet line. Returns True when it went through.

        Deliberately *not* routed through ``sync_lead``/``is_syncable``: /undo of a
        creation marks the lead deleted, and dead rows must be refused by the normal
        sync path — yet the line still has to leave the table. The line itself is
        kept (empty) so lead numbering and the Apps Script append contract are
        untouched.

        *lead_id* is forwarded so the backend can verify the line before blanking it
        (the undo carries the ID of the lead whose row it wants cleared).
        """
        if not row:
            return False
        if not self.configured:
            log_json(
                logger, 30, "sheets clear skipped (not configured)",
                sheet_row=row, action="sheets_clear",
            )
            return False

        values = [""] * COLUMN_COUNT
        async with self._lock:
            for attempt in range(1, RETRY_MAX + 1):
                try:
                    await self._update(row, values, lead_id=lead_id)
                    log_json(
                        logger, 20, "sheets row cleared",
                        sheet_row=row, action="sheets_clear",
                    )
                    return True
                except Exception as exc:  # 429/5xx/network — retry with backoff
                    log_json(
                        logger, 30, "sheets clear retry",
                        sheet_row=row, action="sheets_clear_retry", reason=str(exc),
                    )
                    if attempt < RETRY_MAX:
                        await asyncio.sleep(RETRY_BASE_DELAY * (2 ** (attempt - 1)))
            log_json(
                logger, 40, "sheets clear failed after retries",
                sheet_row=row, action="sheets_clear",
            )
            return False

    async def _append(self, values: list) -> int:
        raise NotImplementedError

    async def _update(self, row: int, values: list, lead_id: int | None = None) -> int | None:
        """Write *values* into the row of *lead_id*; returns the row actually written.

        ``None`` means "the backend could not tell" — callers keep the cached number.
        """
        raise NotImplementedError

    async def close(self) -> None:
        """Release backend resources; no-op unless a subclass owns an http client."""


class SheetsSyncService(BaseSheetsSyncService):
    """Syncs leads to Google Sheets using gspread (blocking calls offloaded to threads)."""

    def __init__(self, sheet_id: str, service_account_json_b64: str):
        super().__init__()
        self.sheet_id = sheet_id
        self._credentials_b64 = service_account_json_b64
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
            # Appending is idempotent by ID, exactly like the Apps Script backend
            # (FIX-8): a lost response makes the client retry, and the retry used to
            # add a second line for the same lead. Look the ID up in column A first.
            lead_id = lead_id_from_values(values)
            if lead_id:
                existing = find_row_by_id(ws.col_values(1), lead_id)
                if existing is not None:
                    log_json(
                        logger, 30, "append skipped — the lead already has a row",
                        lead_id=lead_id, action="sheets_append_duplicate",
                        sheet_row=existing,
                    )
                    return existing
            ws.append_row(values, value_input_option="USER_ENTERED")
            # row index == current row count (header in row 1)
            return len(ws.get_all_values())

        return await asyncio.to_thread(_run)

    async def _update(self, row: int, values: list, lead_id: int | None = None) -> int | None:
        def _run():
            ws = self._worksheet_sync()
            target = row if lead_id is None else resolve_row_for_lead(
                ws.col_values(1), row, lead_id
            )
            ws.update(f"A{target}:AA{target}", [values], value_input_option="USER_ENTERED")
            return target

        return await asyncio.to_thread(_run)


class WebhookSheetsSyncService(BaseSheetsSyncService):
    """Syncs leads to an Apps Script Web App (no service account / GCP billing)."""

    def __init__(self, webhook_url: str, webhook_token: str):
        super().__init__()
        self.webhook_url = webhook_url
        self.webhook_token = webhook_token
        # Apps Script /exec ALWAYS answers a POST with a 302 redirect to
        # script.googleusercontent.com/macros/echo?user_content_key=... — the 302
        # itself carries an empty body, and the real JSON payload is served only by
        # the redirect target. The Content-Type does not change this: text/plain is
        # harmless (and keeps the payload readable by doPost via e.postData.contents),
        # but the client MUST follow the redirect. Otherwise response.json() sees an
        # empty body, every attempt fails, and the retry loop writes N duplicates.
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
        )

    @property
    def configured(self) -> bool:
        return bool(self.webhook_url)

    async def close(self) -> None:
        await self._client.aclose()

    async def _append(self, values: list) -> int:
        row = await self._request("append", values=values)
        return row

    async def _update(self, row: int, values: list, lead_id: int | None = None) -> int | None:
        """Send the update with the lead ID so the script can verify the row.

        The script answers ``{"ok":true,"row":N}`` with the row it really wrote. It
        re-targets the row by ID when the cached number no longer holds this lead,
        and refuses to write at all (``ok:false``) when the lead is not in the table.
        """
        return await self._request("update", row=row, values=values, lead_id=lead_id)

    async def _request(
        self, action: str, values: list, row: int | None = None, lead_id: int | None = None
    ) -> int | None:
        payload = json.dumps(
            {
                "token": self.webhook_token,
                "action": action,
                "row": row,
                # Extra field for the script's row-ownership check. An older deployment
                # simply ignores it and keeps writing to ``row`` (graceful degradation
                # to the pre-FIX-7 behaviour) — hence no version negotiation here.
                "lead_id": lead_id,
                "values": values,
            },
            ensure_ascii=False,
        )
        try:
            response = await self._client.post(
                self.webhook_url,
                content=payload.encode("utf-8"),
                headers={"Content-Type": "text/plain; charset=utf-8"},
            )
        except httpx.HTTPError as exc:
            raise SheetsError(f"webhook network error: {exc}") from exc

        if response.status_code >= 400:
            raise SheetsError(f"webhook HTTP {response.status_code}: {response.text[:200]}")

        try:
            data = response.json()
        except ValueError as exc:
            raise SheetsError(f"webhook response is not JSON: {response.text[:200]}") from exc

        if not isinstance(data, dict) or not data.get("ok"):
            error = data.get("error") if isinstance(data, dict) else None
            raise SheetsError(error or "webhook returned ok=false")

        actual = data.get("row")
        if action == "append":
            if not isinstance(actual, int):
                raise SheetsError("webhook append response missing 'row'")
            return actual
        if action == "update":
            # An older script answers a bare {"ok":true}: keep trusting the cache
            # (degradation), a newer one reports the row it actually wrote.
            if isinstance(actual, int) and actual > 0:
                return actual
            return row
        return None


def build_sheets_service(settings) -> BaseSheetsSyncService:
    """Select the sync backend from configuration.

    Priority: gspread (service account) → Apps Script webhook → not configured.
    """
    if settings.GOOGLE_SERVICE_ACCOUNT_JSON:
        return SheetsSyncService(
            settings.GOOGLE_SHEET_ID, settings.GOOGLE_SERVICE_ACCOUNT_JSON
        )
    if settings.GOOGLE_SHEETS_WEBHOOK_URL:
        return WebhookSheetsSyncService(
            settings.GOOGLE_SHEETS_WEBHOOK_URL, settings.GOOGLE_SHEETS_WEBHOOK_TOKEN
        )
    return SheetsSyncService("", "")
