/**
 * Test harness for docs/google_apps_script_webhook.gs — runs the *real* script in a
 * Node VM against a stubbed Apps Script API (SpreadsheetApp / ContentService) and
 * prints what it did, as JSON on stdout.
 *
 * There is no live Apps Script deployment in CI and no way to run the .gs logic in
 * pytest, so this is the closest honest check of the row-ownership branch: the same
 * source file, the same code path, a fake sheet. It does NOT cover the real Google
 * Sheets service, the deployment or the /exec redirect — see tests/test_sheet_row_identity.py
 * for the client side.
 *
 * Usage: node tests/apps_script_harness.js <path-to-webhook.gs>
 */
"use strict";

const fs = require("fs");
const vm = require("vm");

const COLUMN_COUNT = 27;
const HEADER = ["ID"].concat(Array.from({ length: COLUMN_COUNT - 1 }, (_, i) => "h" + (i + 2)));
const LEAD = ["1"].concat(Array.from({ length: COLUMN_COUNT - 1 }, () => "lead"));
const OTHER = ["9"].concat(Array.from({ length: COLUMN_COUNT - 1 }, () => "other"));
const BLANK = Array.from({ length: COLUMN_COUNT }, () => "");

function makeSheet(rows, recorder) {
  const data = rows.map((row) => row.slice());

  function cell(row, col) {
    const line = data[row - 1];
    if (!line || line[col - 1] === undefined || line[col - 1] === null) return "";
    return line[col - 1];
  }

  function makeRange(row, col, numRows, numCols) {
    return {
      getValue: () => cell(row, col),
      getValues: () => {
        const out = [];
        for (let r = 0; r < numRows; r++) {
          const line = [];
          for (let c = 0; c < numCols; c++) line.push(cell(row + r, col + c));
          out.push(line);
        }
        return out;
      },
      setValues: (matrix) => {
        recorder.writes.push({ row: row, values: matrix[0] });
        for (let r = 0; r < matrix.length; r++) {
          for (let c = 0; c < matrix[r].length; c++) {
            while (data.length < row + r) data.push([]);
            while (data[row + r - 1].length < col + c) data[row + r - 1].push("");
            data[row + r - 1][col + c - 1] = matrix[r][c];
          }
        }
      },
      createTextFinder: (needle) => {
        let hit = null;
        for (let r = row; r < row + numRows && !hit; r++) {
          for (let c = col; c < col + numCols && !hit; c++) {
            if (String(cell(r, c)) === String(needle)) hit = r;
          }
        }
        return {
          matchEntireCell: () => ({ findNext: () => (hit === null ? null : { getRow: () => hit }) }),
        };
      },
    };
  }

  const sheet = {
    // Apps Script semantics: the last row that holds any content.
    getLastRow: () => {
      let last = 0;
      data.forEach((line, index) => {
        if (line && line.some((value) => String(value).trim() !== "")) last = index + 1;
      });
      return last;
    },
    getRange: (row, col, numRows, numCols) =>
      makeRange(row, col, numRows || 1, numCols || 1),
    appendRow: (values) => {
      recorder.appends.push(values.slice());
      data.push(values.slice());
    },
    dump: () => data.map((line) => line.slice()),
  };
  return sheet;
}

function run(source, scenario) {
  const recorder = { writes: [], appends: [] };
  const sheet = makeSheet(scenario.rows, recorder);
  const spreadsheet = {
    getSheetByName: () => sheet,
    getActiveSheet: () => sheet,
  };
  let output = null;

  const sandbox = {
    SpreadsheetApp: {
      getActiveSpreadsheet: () => spreadsheet,
      openById: () => spreadsheet,
    },
    ContentService: {
      MimeType: { JSON: "json" },
      createTextOutput: () => ({
        setMimeType: () => {},
        setContent: (text) => {
          output = text;
        },
      }),
    },
    JSON: JSON,
    String: String,
    Array: Array,
    Object: Object,
    Number: Number,
    console: console,
  };

  vm.createContext(sandbox);
  vm.runInContext(source, sandbox, { filename: "google_apps_script_webhook.gs" });
  // The file ships a placeholder token; the harness supplies its own.
  sandbox.TOKEN = "test-token";

  const payload = Object.assign({ token: "test-token" }, scenario.payload);
  const response = sandbox.doPost({ postData: { contents: JSON.stringify(payload) } });

  return {
    name: scenario.name,
    response: output === null ? null : JSON.parse(output),
    writes: recorder.writes.map((write) => ({ row: write.row, values: write.values })),
    appends: recorder.appends,
    rows: sheet.dump(),
    returned_content: response === null ? null : typeof response,
  };
}

function main() {
  const path = process.argv[2];
  if (!path) {
    console.error("usage: node apps_script_harness.js <path-to-webhook.gs>");
    process.exit(2);
  }
  const source = fs.readFileSync(path, "utf8");

  const scenarios = [
    // FIX-7: the cached row still holds this lead → write it and report the row.
    { name: "update_row_matches", rows: [HEADER, LEAD], payload: { action: "update", row: 2, lead_id: 1, values: LEAD } },
    // FIX-7: a line was inserted by hand → row 2 is another lead, ours is on row 3.
    { name: "update_row_shifted", rows: [HEADER, OTHER, LEAD], payload: { action: "update", row: 2, lead_id: 1, values: LEAD } },
    // FIX-7: the lead is nowhere in column A → refuse, never write over the stranger.
    { name: "update_lead_missing", rows: [HEADER, OTHER], payload: { action: "update", row: 2, lead_id: 1, values: LEAD } },
    // Compatibility: an old client sends no lead_id → previous behaviour (write to row).
    { name: "update_without_lead_id", rows: [HEADER, OTHER], payload: { action: "update", row: 2, values: LEAD } },
    // /undo retry after a lost response: the line is already blank → success, no write.
    { name: "clear_retry_blank", rows: [HEADER, BLANK], payload: { action: "update", row: 2, lead_id: 1, values: BLANK } },
    // FIX-8: appending an ID that is already in the table must not add a line.
    { name: "append_duplicate", rows: [HEADER, LEAD], payload: { action: "append", values: LEAD } },
    { name: "append_new", rows: [HEADER, LEAD], payload: { action: "append", values: OTHER } },
  ];

  const results = scenarios.map((scenario) => run(source, scenario));
  process.stdout.write(JSON.stringify(results, null, 1));
}

main();
