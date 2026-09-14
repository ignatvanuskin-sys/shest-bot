/**
 * LeadForge AI — Google Sheets webhook (Apps Script).
 *
 * Как деплоить (2 минуты, без GCP/карты):
 *   1. Откройте целевую таблицу → Расширения → Apps Script.
 *   2. Вставьте этот код целиком и замените TOKEN на свой случайный секрет.
 *   3. Деплой → Новое развертывание → тип «Веб-приложение».
 *      - «Выполнять как»: я
 *      - «Доступ»: все, у кого есть ссылка
 *   4. Скопируйте URL /exec в GOOGLE_SHEETS_WEBHOOK_URL, а TOKEN — в
 *      GOOGLE_SHEETS_WEBHOOK_TOKEN.
 *
 * Протокол: python-клиент шлёт POST с Content-Type: text/plain, телом — JSON-строкой
 * (важно: НЕ application/json — иначе /exec ответит 302-редиректом). Скрипт читает
 * e.postData.contents.
 */

var TOKEN = "ЗАМЕНИТЕ_НА_СВОЙ_ТОКЕН";
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

function getSheet() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName(SHEET_NAME);
  if (!sheet) {
    sheet = ss.getActiveSheet();
  }
  return sheet;
}
