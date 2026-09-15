"""FIX-7 / FIX-8: a sheet row is written only after its identity is confirmed.

``lead.sheet_row`` is a cache of a row *number*. Inserting or deleting one line in the
table by hand shifts every line below it, and both backends used to write to the bare
number — overwriting whichever lead had moved onto it. Appending had the mirror-image
problem: a lost response made the retry add a second line for the same lead.

Covered here:

* the Apps Script client sends ``lead_id`` with every update and honours the row the
  script reports back (including "the lead moved, I wrote row N");
* a refusal from the script (``ok:false`` — the lead is nowhere in column A) fails the
  sync instead of falling back to the cached number;
* an older deployment that ignores ``lead_id`` and answers a bare ``{"ok":true}``
  still works — the client keeps trusting the cache (graceful degradation);
* the gspread backend verifies column A itself before writing, and re-finds the row;
* appending through gspread is idempotent by ID, so a retry after a lost response
  reuses the existing line.

There is no live Apps Script in CI: the webhook tests drive the real client against an
in-memory mirror of the deployed script's update/append branch (``FakeAppsScript``).
That pins the *protocol*; the JavaScript itself is only as good as that mirror.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app import models
from app.services import sheets as sheets_mod
from app.services.sheets import (
    COLUMN_COUNT,
    SheetsError,
    SheetsSyncService,
    WebhookSheetsSyncService,
    find_row_by_id,
    lead_to_row_values,
    resolve_row_for_lead,
    row_holds_lead,
)

TOKEN = "secret"
HEADER = ["ID"] + [f"col{index}" for index in range(2, COLUMN_COUNT + 1)]
OTHER_VALUES = ["9"] + [f"other{index}" for index in range(2, COLUMN_COUNT + 1)]
LEAD_VALUES = ["1"] + [f"lead{index}" for index in range(2, COLUMN_COUNT + 1)]


# ---------------- pure helpers ----------------


def test_row_holds_lead_compares_column_a_only():
    column_a = ["ID", "1", "2"]

    assert row_holds_lead(column_a, 2, 1) is True
    assert row_holds_lead(column_a, 3, 1) is False
    assert row_holds_lead(column_a, 0, 1) is False, "row numbers are 1-based"
    assert row_holds_lead(column_a, 99, 1) is False, "beyond the last row is not a match"


def test_find_row_by_id_skips_the_header_row():
    column_a = ["ID", "1", "2", "1"]

    assert find_row_by_id(column_a, 1) == 2, "the first match after the header wins"
    assert find_row_by_id(column_a, 2) == 3
    assert find_row_by_id(column_a, 7) is None
    assert find_row_by_id(column_a, "") is None


def test_resolve_row_for_lead_prefers_the_verified_number_then_searches():
    # Row 2 really is lead 1 → trust the cache.
    assert resolve_row_for_lead(["ID", "1", "2"], 2, 1) == 2
    # Row 2 belongs to lead 9 now (a line was inserted above) → find lead 1 on row 3.
    assert resolve_row_for_lead(["ID", "9", "1"], 2, 1) == 3


def test_resolve_row_for_lead_refuses_when_the_lead_is_missing():
    with pytest.raises(SheetsError) as exc:
        resolve_row_for_lead(["ID", "9", "8"], 2, 1)

    assert "не принадлежит лиду 1" in str(exc.value)


# ---------------- webhook backend: the Apps Script contract ----------------


class FakeAppsScript:
    """In-memory mirror of the update/append branch of ``docs/google_apps_script_webhook.gs``.

    Row 1 holds the headers; ``rows[i]`` is sheet row ``i + 2``. Every write is
    recorded, so a test can prove that a foreign line was never touched.
    """

    def __init__(self, rows: list[list[str]] | None = None):
        self.rows: list[list[str]] = [list(HEADER)] + [list(row) for row in (rows or [])]
        self.updates: list[tuple[int, list]] = []
        self.appends: list[list] = []
        self.requests: list[dict] = []

    # ---- sheet accessors used by the mirrored script logic ----
    def _last_row(self) -> int:
        return len(self.rows)

    def _cell(self, row: int) -> str:
        if row < 1 or row > self._last_row():
            return ""
        cells = self.rows[row - 1]
        return str(cells[0]) if cells else ""

    def _holds(self, row: int, lead_id) -> bool:
        value = self._cell(row)
        return value.strip() != "" and value.strip() == str(lead_id).strip()

    def _find(self, lead_id):
        for index in range(2, self._last_row() + 1):
            if self._holds(index, lead_id):
                return index
        return None

    def _is_blank(self, row: int) -> bool:
        if row < 1 or row > self._last_row():
            return True
        return all(str(cell).strip() == "" for cell in self.rows[row - 1])

    def _write(self, row: int, values: list) -> None:
        self.updates.append((row, list(values)))
        while len(self.rows) < row:
            self.rows.append([""] * COLUMN_COUNT)
        self.rows[row - 1] = list(values)

    # ---- httpx handler ----
    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        self.requests.append(payload)

        if payload.get("token") != TOKEN:
            return httpx.Response(200, json={"ok": False, "error": "invalid token"})

        action = payload["action"]
        values = payload["values"]

        if action == "append":
            lead_id = values[0]
            existing = self._find(lead_id) if str(lead_id).strip() else None
            if existing is not None:
                return httpx.Response(200, json={"ok": True, "row": existing, "duplicate": True})
            self.rows.append(list(values))
            self.appends.append(list(values))
            return httpx.Response(200, json={"ok": True, "row": self._last_row()})

        if action == "update":
            row = payload["row"]
            lead_id = payload.get("lead_id")
            if lead_id is not None and str(lead_id).strip() != "":
                if not self._holds(row, lead_id):
                    found = self._find(lead_id)
                    if found is not None:
                        row = found  # the line moved: write where the lead really is
                    elif all(str(value).strip() == "" for value in values) and self._is_blank(payload["row"]):
                        return httpx.Response(
                            200, json={"ok": True, "row": payload["row"], "blank": True}
                        )
                    else:
                        return httpx.Response(
                            200, json={"ok": False, "error": f"row not found for id {lead_id}"}
                        )
            self._write(row, values)
            return httpx.Response(200, json={"ok": True, "row": row})

        return httpx.Response(200, json={"ok": False, "error": f"unknown action: {action}"})


def _script_service(fake: FakeAppsScript) -> tuple[WebhookSheetsSyncService, httpx.AsyncClient]:
    svc = WebhookSheetsSyncService("http://example.test/exec", TOKEN)
    service_client = svc._client
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(fake.handler),
        follow_redirects=service_client.follow_redirects,
    )
    svc._client = client
    return svc, service_client


async def test_webhook_update_carries_lead_id():
    svc = WebhookSheetsSyncService("http://example.test/exec", TOKEN)
    svc._client = _StubClient([_StubResponse(200, {"ok": True, "row": 7})])

    row = await svc._update(7, LEAD_VALUES, lead_id=1)

    body = json.loads(svc._client.calls[0]["content"].decode("utf-8"))
    assert body["action"] == "update"
    assert body["lead_id"] == 1, "the script cannot verify the row without the lead id"
    assert row == 7


async def test_webhook_update_retargets_a_shifted_row_and_repairs_the_cache():
    """A line was inserted by hand: cached row 2 now holds lead 9, lead 1 sits on row 3."""
    fake = FakeAppsScript(rows=[OTHER_VALUES, LEAD_VALUES])
    svc, service_client = _script_service(fake)
    lead = models.Lead(id=1, sheet_row=2)

    try:
        row = await svc.sync_lead(lead)
    finally:
        await svc.close()
        await service_client.aclose()

    assert fake.updates == [(3, lead_to_row_values(lead))], "the foreign row must not be written"
    assert row == 3
    assert lead.sheet_row == 3, "the repaired row number must replace the stale cache"


async def test_webhook_update_refuses_when_the_lead_is_not_in_the_table(monkeypatch):
    """The cached row holds another lead and ours is gone → no write, sync fails."""
    monkeypatch.setattr(sheets_mod, "RETRY_BASE_DELAY", 0.0)
    fake = FakeAppsScript(rows=[OTHER_VALUES])
    svc, service_client = _script_service(fake)
    lead = models.Lead(id=1, sheet_row=2)

    try:
        row = await svc.sync_lead(lead)
    finally:
        await svc.close()
        await service_client.aclose()

    assert row is None, "a refused update must be reported as a failure"
    assert fake.updates == [], "nothing may be written over another lead's line"
    assert lead.sheet_row == 2, "the cache is untouched when the sync fails"
    assert fake.rows[1] == OTHER_VALUES, "the other lead's line survived"
    errors = [request for request in fake.requests if request["action"] == "update"]
    assert len(errors) == sheets_mod.RETRY_MAX, "the failure is retried, then given up"


async def test_webhook_clear_only_blanks_the_row_that_holds_the_lead():
    """/undo passes the lead id too: it must not blank a foreign line after a shift."""
    fake = FakeAppsScript(rows=[OTHER_VALUES, LEAD_VALUES])
    svc, service_client = _script_service(fake)

    try:
        cleared = await svc.clear_row(2, lead_id=1)
    finally:
        await svc.close()
        await service_client.aclose()

    assert cleared is True
    assert fake.updates == [(3, [""] * COLUMN_COUNT)]
    assert fake.rows[1] == OTHER_VALUES, "the row that moved into the cached slot is intact"
    assert fake.rows[2] == [""] * COLUMN_COUNT


async def test_webhook_update_works_with_an_old_script_that_ignores_lead_id():
    """Deployment lag: the script answers a bare {"ok":true} — do not break on it."""
    svc = WebhookSheetsSyncService("http://example.test/exec", TOKEN)
    svc._client = _StubClient([_StubResponse(200, {"ok": True})])
    lead = models.Lead(id=1, sheet_row=7)

    row = await svc.sync_lead(lead)

    assert row == 7, "without a reported row the cache stays the source of the number"
    assert lead.sheet_row == 7
    body = json.loads(svc._client.calls[0]["content"].decode("utf-8"))
    assert body["lead_id"] == 1, "the extra field is sent regardless (an old script ignores it)"
    assert body["row"] == 7


async def test_webhook_append_is_idempotent_in_the_script_contract():
    """FIX-8 for the webhook path lives in the script: a duplicate reports the row."""
    fake = FakeAppsScript(rows=[LEAD_VALUES])
    svc, service_client = _script_service(fake)

    try:
        row = await svc._append(LEAD_VALUES)
    finally:
        await svc.close()
        await service_client.aclose()

    assert row == 2
    assert fake.appends == [], "the script must not add a second line for the same id"


# ---------------- gspread backend ----------------


class FakeWorksheet:
    """Minimal gspread worksheet double: column A, row writes, appends."""

    def __init__(self, rows: list[list] | None = None):
        self.rows: list[list] = [list(HEADER)] + [list(row) for row in (rows or [])]
        self.updates: list[tuple[str, list]] = []
        self.appends: list[list] = []

    def col_values(self, index: int) -> list:
        return [row[index - 1] if len(row) >= index else "" for row in self.rows]

    def update(self, range_name: str, values: list, value_input_option: str | None = None) -> None:
        self.updates.append((range_name, values))
        row = int(range_name.split(":")[0][1:])
        while len(self.rows) < row:
            self.rows.append([""] * COLUMN_COUNT)
        self.rows[row - 1] = list(values[0])

    def append_row(self, values: list, value_input_option: str | None = None) -> None:
        self.appends.append(list(values))
        self.rows.append(list(values))

    def get_all_values(self) -> list[list]:
        return [list(row) for row in self.rows]


def _gspread_service(ws: FakeWorksheet, monkeypatch) -> SheetsSyncService:
    svc = SheetsSyncService("sheetid", "e30=")  # base64 of "{}"
    monkeypatch.setattr(svc, "_worksheet_sync", lambda: ws)
    return svc


async def test_gspread_update_writes_to_the_row_that_holds_the_lead(monkeypatch):
    ws = FakeWorksheet(rows=[OTHER_VALUES, LEAD_VALUES])
    svc = _gspread_service(ws, monkeypatch)
    lead = models.Lead(id=1, sheet_row=2)

    row = await svc.sync_lead(lead)

    assert row == 3
    assert lead.sheet_row == 3
    assert ws.updates == [("A3:AA3", [lead_to_row_values(lead)])], "the foreign row must not be written"
    assert ws.rows[1] == OTHER_VALUES


async def test_gspread_update_refuses_a_row_that_is_not_the_leads(monkeypatch):
    monkeypatch.setattr(sheets_mod, "RETRY_BASE_DELAY", 0.0)
    monkeypatch.setattr(sheets_mod, "RETRY_MAX", 2)
    ws = FakeWorksheet(rows=[OTHER_VALUES])
    svc = _gspread_service(ws, monkeypatch)
    lead = models.Lead(id=1, sheet_row=2)

    row = await svc.sync_lead(lead)

    assert row is None
    assert ws.updates == []
    assert lead.sheet_row == 2


async def test_gspread_update_keeps_the_cached_row_when_it_is_correct(monkeypatch):
    ws = FakeWorksheet(rows=[LEAD_VALUES])
    svc = _gspread_service(ws, monkeypatch)
    lead = models.Lead(id=1, sheet_row=2)

    row = await svc.sync_lead(lead)

    assert row == 2
    assert ws.updates == [("A2:AA2", [lead_to_row_values(lead)])]


async def test_gspread_append_adds_a_line_when_the_id_is_new(monkeypatch):
    ws = FakeWorksheet()
    svc = _gspread_service(ws, monkeypatch)

    row = await svc._append(LEAD_VALUES)

    assert row == 2
    assert ws.appends == [LEAD_VALUES]


async def test_gspread_append_reuses_the_existing_line(monkeypatch):
    """FIX-8: the same lead must never get a second line."""
    ws = FakeWorksheet(rows=[LEAD_VALUES])
    svc = _gspread_service(ws, monkeypatch)

    row = await svc._append(LEAD_VALUES)

    assert row == 2
    assert ws.appends == [], "the retry appended a duplicate row"


async def test_gspread_append_retry_after_a_lost_response_does_not_duplicate(monkeypatch):
    """End-to-end: the write reached the table, the response did not come back."""
    monkeypatch.setattr(sheets_mod, "RETRY_BASE_DELAY", 0.0)
    ws = FakeWorksheet()
    svc = _gspread_service(ws, monkeypatch)
    real_append = svc._append
    attempts = {"n": 0}

    async def flaky(values):
        attempts["n"] += 1
        if attempts["n"] == 1:
            ws.append_row(values)  # the table got the row…
            raise SheetsError("response lost")  # …but the client never learned the row
        return await real_append(values)

    monkeypatch.setattr(svc, "_append", flaky)
    lead = models.Lead(id=1)

    row = await svc.sync_lead(lead)

    assert attempts["n"] == 2
    assert row == 2
    assert lead.sheet_row == 2
    assert len(ws.rows) == 2, f"expected header + one line, got {ws.rows}"


async def test_gspread_append_without_an_id_skips_the_lookup(monkeypatch):
    """values[0] empty is the documented «append without checking» case."""
    ws = FakeWorksheet(rows=[LEAD_VALUES])
    svc = _gspread_service(ws, monkeypatch)

    row = await svc._append([""] * COLUMN_COUNT)

    assert row == 3, "an anonymous row is appended at the end"
    assert len(ws.appends) == 1


# ---------------- small httpx doubles for the scripted cases ----------------


class _StubResponse:
    def __init__(self, status_code: int, data: dict):
        self.status_code = status_code
        self._data = data
        self.text = json.dumps(data)

    def json(self):
        return self._data


class _StubClient:
    def __init__(self, responses: list):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def post(self, url, content=None, headers=None):
        self.calls.append({"url": url, "content": content, "headers": headers})
        return self.responses.pop(0)

    async def aclose(self):
        pass
