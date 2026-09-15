"""Execute the deployed Apps Script logic against a stubbed Sheets API.

The webhook backend is split in two halves: the Python client (covered by
``test_sheet_row_identity.py``) and ``docs/google_apps_script_webhook.gs``, which runs
in Google's cloud and cannot be reached from CI. This module runs the *actual* .gs
source in a Node VM with a fake ``SpreadsheetApp`` (``tests/apps_script_harness.js``),
so the row-ownership branch is executed rather than only mirrored.

What this proves: give the script a table where the cached row belongs to another lead,
and it writes to the row that really holds this lead (or refuses). What it does not
prove: the real Google Sheets API, the deployment, quotas and the /exec redirect.

The module is skipped when Node is unavailable; the harness file is plain JS with no
dependencies.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
APPS_SCRIPT = REPO_ROOT / "docs" / "google_apps_script_webhook.gs"
HARNESS = Path(__file__).resolve().parent / "apps_script_harness.js"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


@pytest.fixture(scope="module")
def outcomes() -> dict[str, dict]:
    """Run every scenario in the real .gs once and index the results by name."""
    assert APPS_SCRIPT.exists(), f"Apps Script source missing: {APPS_SCRIPT}"
    completed = subprocess.run(
        [NODE, str(HARNESS), str(APPS_SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(REPO_ROOT),
        timeout=60,
    )
    assert completed.returncode == 0, f"Apps Script harness failed:\n{completed.stderr}"
    results = json.loads(completed.stdout)
    return {item["name"]: item for item in results}


def test_update_writes_the_cached_row_when_it_holds_the_lead(outcomes):
    result = outcomes["update_row_matches"]

    assert result["response"] == {"ok": True, "row": 2}
    assert [(write["row"], write["values"][0]) for write in result["writes"]] == [(2, "1")]


def test_update_retargets_a_shifted_row(outcomes):
    """A hand-inserted line moved the lead from row 2 to row 3."""
    result = outcomes["update_row_shifted"]

    assert result["response"] == {"ok": True, "row": 3}
    assert [(write["row"], write["values"][0]) for write in result["writes"]] == [(3, "1")], (
        "the script wrote to the row it was told, not to the row that owns the lead"
    )
    assert result["rows"][1][0] == "9", "the other lead's line must be untouched"


def test_update_refuses_when_the_lead_is_not_in_column_a(outcomes):
    result = outcomes["update_lead_missing"]

    assert result["response"] == {"ok": False, "error": "row not found for id 1"}
    assert result["writes"] == [], "refusing must mean writing nothing at all"
    assert result["rows"][1][0] == "9", "the foreign row survived"


def test_update_without_lead_id_keeps_the_old_behaviour(outcomes):
    """An older client (no lead_id) is still served: write the row it asks for."""
    result = outcomes["update_without_lead_id"]

    assert result["response"] == {"ok": True, "row": 2}
    assert [write["row"] for write in result["writes"]] == [2]


def test_repeated_clear_of_an_already_blank_row_reports_success(outcomes):
    """The /undo retry after a lost response must not become a hard error."""
    result = outcomes["clear_retry_blank"]

    assert result["response"] == {"ok": True, "row": 2, "blank": True}
    assert result["writes"] == [], "nothing left to blank"


def test_append_is_idempotent_by_lead_id(outcomes):
    result = outcomes["append_duplicate"]

    assert result["response"] == {"ok": True, "row": 2, "duplicate": True}
    assert result["appends"] == [], "the retry added a second line"


def test_append_adds_a_line_for_a_new_lead(outcomes):
    result = outcomes["append_new"]

    assert result["response"] == {"ok": True, "row": 3}
    assert len(result["appends"]) == 1
