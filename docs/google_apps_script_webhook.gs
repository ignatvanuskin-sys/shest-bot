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
 * Протокол: python-клиент шлёт POST с Content-Type: text/plain, телом — JSON-строкой.
 * Скрипт читает e.postData.contents.
 *
 * ВАЖНО про редирект: Apps Script /exec отвечает на POST HTTP 302 с пустым телом и
 * Location: https://script.googleusercontent.com/macros/echo?user_content_key=...
 * — сам JSON отдаётся только по адресу из Location. Content-Type на это не влияет:
 * text/plain безвреден, но клиент ОБЯЗАН следовать редиректу, иначе получит пустое
 * тело. Python-клиент (app/services/sheets.py) создаёт httpx.AsyncClient с
 * follow_redirects=True именно поэтому. Если редирект не пройден, клиент считает
 * запрос неуспешным и повторяет его — поэтому append здесь идемпотентен по ID.
 *
 * Идемпотентность append: перед добавлением строки скрипт ищет values[0] (ID лида)
 * в колонке A целевого листа. Если строка с таким ID уже есть — новая НЕ
 * добавляется, а возвращается {"ok":true,"row":<номер существующей строки>,
 * "duplicate":true}. Пустой values[0] означает «добавить без проверки».
 * Поиск идёт по диапазону колонки A (TextFinder), а не по всему листу.
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

      // Идемпотентность: повторная попытка (ретрай клиента) не должна создавать
      // дубль. Ищем ID только в колонке A, начиная со строки 2 (строка 1 — заголовки).
      var leadId = values[0];
      if (leadId !== "" && leadId !== null && leadId !== undefined) {
        var existingRow = findRowById(sheet, leadId);
        if (existingRow > 0) {
          out.setContent(JSON.stringify({ ok: true, row: existingRow, duplicate: true }));
          return out;
        }
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

/**
 * Ищет строку с точным значением ID в колонке A.
 * Возвращает номер строки или 0, если не найдено.
 * Строка 1 (заголовки) не участвует в поиске. Поиск ограничен колонкой A,
 * чтобы не вычитывать весь лист.
 */
function findRowById(sheet, leadId) {
  var lastRow = sheet.getLastRow();
  if (lastRow < 2) {
    return 0;
  }
  var idColumn = sheet.getRange(2, 1, lastRow - 1, 1);
  var found = idColumn
    .createTextFinder(String(leadId))
    .matchEntireCell(true)
    .findNext();
  return found ? found.getRow() : 0;
}
