/**
 * LeadForge AI — Google Sheets webhook (Apps Script).
 *
 * Скрипт поддерживает два варианта развёртывания.
 *
 * Вариант 1 — скрипт создан ИЗ таблицы (контейнерный):
 *   1. Откройте целевую таблицу → Расширения → Apps Script.
 *   2. Вставьте этот код целиком и замените TOKEN на свой случайный секрет.
 *   3. SPREADSHEET_ID можно оставить пустым ("") — таблица определяется
 *      автоматически как активная таблица контейнера.
 *   4. Деплой → Новое развертывание → тип «Веб-приложение».
 *      - «Выполнять как»: я
 *      - «Доступ»: все, у кого есть ссылка
 *   5. Скопируйте URL /exec в GOOGLE_SHEETS_WEBHOOK_URL, а TOKEN — в
 *      GOOGLE_SHEETS_WEBHOOK_TOKEN.
 *
 * Вариант 2 — скрипт создан ОТДЕЛЬНО от таблицы (standalone, script.google.com):
 *   1. Откройте script.google.com → Новый проект.
 *   2. Вставьте этот код целиком и замените TOKEN на свой случайный секрет.
 *   3. Обязательно заполните SPREADSHEET_ID: это ID таблицы из её адреса —
 *      часть между /d/ и /edit, например для адреса
 *        https://docs.google.com/spreadsheets/d/1AbCdEf1234567890/edit#gid=0
 *      ID будет 1AbCdEf1234567890.
 *      Без SPREADSHEET_ID standalone-скрипт не видит таблицу и вернёт ошибку.
 *   4. Деплой → Новое развертывание → тип «Веб-приложение» (те же настройки,
 *      что и в варианте 1).
 *
 * Как проверить, что деплой работает:
 *   Откройте URL /exec прямо в браузере (GET-запрос). Должен вернуться JSON
 *   {"ok":true,"service":"leadforge-webhook"} — это значит, что приложение
 *   развёрнуто и доступно. Если вместо этого страница логина Google — проверьте,
 *   что «Доступ» выставлен в «все, у кого есть ссылка». Если браузер показывает
 *   {"ok":false,...} — скрипт развёрнут, но не видит таблицу: проверьте
 *   SPREADSHEET_ID и имя листа SHEET_NAME.
 *
 * Протокол: python-клиент шлёт POST с Content-Type: text/plain, телом — JSON-строкой
 * (важно: НЕ application/json — иначе /exec ответит 302-редиректом). Скрипт читает
 * e.postData.contents.
 */

var TOKEN = "ЗАМЕНИТЕ_НА_СВОЙ_ТОКЕН";

// ID таблицы (standalone-развёртывание). Пустая строка = взять активную таблицу
// (работает только если скрипт создан из таблицы через Расширения → Apps Script).
var SPREADSHEET_ID = "";

var SHEET_NAME = "Leads";
var COLUMN_COUNT = 27;

// Порядок колонок фиксирован (A..AA), совпадает с app/services/sheets.py.
var HEADERS = [
  "ID",
  "Дата добавления",
  "Компания",
  "Категория/ниша",
  "Город",
  "Адрес",
  "Телефон",
  "WhatsApp",
  "Email",
  "Instagram",
  "Telegram",
  "Сайт",
  "Источник",
  "Ссылка на источник",
  "Услуги",
  "Теги/марки",
  "Описание",
  "Рейтинг",
  "Кол-во отзывов",
  "Контактное лицо",
  "Статус",
  "Приоритет",
  "Боль клиента",
  "Комментарий",
  "Последнее действие",
  "Дата последнего контакта",
  "⚠️ Требует проверки"
];

function doPost(e) {
  var out = ContentService.createTextOutput();
  out.setMimeType(ContentService.MimeType.JSON);

  try {
    var payload = JSON.parse(e.postData.contents);

    if (payload.token !== TOKEN) {
      out.setContent(JSON.stringify({ ok: false, error: "invalid token" }));
      return out;
    }

    var action = payload.action;
    var values = payload.values;

    if (!Array.isArray(values) || values.length !== COLUMN_COUNT) {
      out.setContent(JSON.stringify({ ok: false, error: "values must have " + COLUMN_COUNT + " items" }));
      return out;
    }

    var sheet = getSheet();

    if (action === "append") {
      if (sheet.getLastRow() === 0) {
        sheet.appendRow(HEADERS);
      }
      sheet.appendRow(values);
      out.setContent(JSON.stringify({ ok: true, row: sheet.getLastRow() }));
      return out;
    }

    if (action === "update") {
      var row = payload.row;
      if (typeof row !== "number" || row < 1) {
        out.setContent(JSON.stringify({ ok: false, error: "row must be a positive integer" }));
        return out;
      }
      sheet.getRange(row, 1, 1, COLUMN_COUNT).setValues([values]);
      out.setContent(JSON.stringify({ ok: true }));
      return out;
    }

    out.setContent(JSON.stringify({ ok: false, error: "unknown action: " + action }));
    return out;
  } catch (err) {
    out.setContent(JSON.stringify({ ok: false, error: String(err) }));
    return out;
  }
}

// GET /exec — проверка развёртывания: откройте URL в браузере.
function doGet(e) {
  var out = ContentService.createTextOutput();
  out.setMimeType(ContentService.MimeType.JSON);
  out.setContent(JSON.stringify({ ok: true, service: "leadforge-webhook" }));
  return out;
}

function getSheet() {
  var ss = SPREADSHEET_ID
    ? SpreadsheetApp.openById(SPREADSHEET_ID)
    : SpreadsheetApp.getActiveSpreadsheet();

  if (!ss) {
    throw new Error(
      "Скрипт не видит таблицу. Укажите SPREADSHEET_ID (ID таблицы — часть адреса " +
      "между /d/ и /edit) либо создайте скрипт из таблицы через Расширения → Apps Script."
    );
  }

  var sheet = ss.getSheetByName(SHEET_NAME);
  if (!sheet) {
    sheet = ss.getActiveSheet();
  }
  return sheet;
}
