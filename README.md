# LeadForge AI

Telegram-бот для сбора и структурирования B2B-лидов из 2GIS / Instagram / сайтов в Google Sheets.

Пользователь копирует информацию о компании (одним или несколькими сообщениями подряд), бот буферизует сообщения
6–8 секунд (или до команды `/done` / кнопки «✅ Готово»), извлекает данные через LLM (OpenRouter), показывает карточку
для проверки, дедуплицирует и пишет строку в Google Sheets. Источник истины — локальная SQLite-база.

## Стек

- Python 3.11+, aiogram 3.x (FSM), FastAPI (webhook-роут)
- SQLAlchemy (async) + SQLite (WAL), Alembic-миграции
- OpenRouter (по умолчанию `openrouter/free` → fallback `nvidia/nemotron-3-ultra-550b-a55b:free`), httpx
- Pydantic v2, phonenumbers (регион KZ), rapidfuzz, gspread + google-auth
- pytest + pytest-asyncio

## Структура проекта

```
app/
  config.py            # настройки из .env
  logging_config.py    # JSON-логи со сквозным session_id
  database.py          # async engine/session, WAL, запуск Alembic
  models.py            # ORM: leads, lead_sessions, raw_messages, extraction_logs, users, audit_log
  di.py                # DI-контейнер (бот, диспетчер, сервисы)
  main.py              # FastAPI app + webhook + dev-polling
  schemas/
    extraction.py      # Pydantic-схема ответа LLM
  services/
    normalize.py       # нормализация телефонов/сайтов/соцсетей/названий
    extraction.py      # LLM-извлечение (retry + fallback + extraction_logs)
    dedup.py           # дедупликация (сильный/средний уровень)
    sheets.py          # синхронизация Google Sheets (append/update, ретраи, экранирование)
    lead_service.py    # бизнес-логика лидов (создание, merge, undo, stats, доступ, очистка логов)
    session_buffer.py  # буфер сообщений с отменяемым таймером
    background.py      # реестр фоновых задач + воркеры (авто-досинхронизация, очистка логов)
    startup.py         # обслуживание на старте (закрытие «зависших» сессий)
  bot/
    states.py          # FSM: Collecting → Reviewing → EditingField / ConfirmingDuplicate / ManualEntry
    keyboards.py       # inline-клавиатуры
    cards.py           # рендер карточки
    flow.py            # оркестрация флоу (буфер → извлечение → дедуп → сохранение)
    handlers.py        # aiogram-хендлеры
    middlewares.py     # allowlist + retry Telegram
    dispatcher.py      # сборка диспетчера, set_webhook, feed_update
alembic/               # миграции
tests/                 # pytest
Dockerfile             # прод-образ (python:3.11-slim) для Railway
railway.toml           # конфиг деплоя: сборка из Dockerfile + healthcheck /health
```

## Настройка

Все секреты — только в `.env` (файл в `.gitignore`, в репозиторий не попадает).

```env
BOT_TOKEN=...                        # токен бота у @BotFather
OPENROUTER_API_KEY=...               # https://openrouter.ai/keys
OPENROUTER_MODEL=                    # пусто = openrouter/free (см. «Модели LLM»)
OPENROUTER_FALLBACK_MODEL=           # пусто = nvidia/nemotron-3-ultra-550b-a55b:free
GOOGLE_SERVICE_ACCOUNT_JSON=...      # base64 от JSON-ключа сервис-аккаунта Google (путь A)
GOOGLE_SHEET_ID=...                  # id таблицы из URL /d/<ID>/edit (путь A)
GOOGLE_SHEETS_WEBHOOK_URL=...        # URL /exec Apps Script Web App (путь B, без GCP)
GOOGLE_SHEETS_WEBHOOK_TOKEN=...      # токен из скрипта Apps Script (путь B)
ALLOWED_USER_IDS=123,456             # через запятую id telegram-пользователей
DATABASE_URL=sqlite+aiosqlite:///./leadforge.db
DEV_POLLING=false                    # true — запуск через long polling (локально)
WEBHOOK_URL=...                      # публичный URL для webhook (production)
WEBHOOK_SECRET=...                   # секрет для заголовка X-Telegram-Bot-Api-Secret-Token
RAILWAY_PUBLIC_DOMAIN=...            # Railway задаёт сам; из него строится WEBHOOK_URL, если он пуст
DEFAULT_CITY=Алматы
COLLECT_TIMEOUT_SECONDS=7
AUTO_RESYNC_INTERVAL_SECONDS=300  # период авто-досинхронизации; 0 — выключить воркер
DISPLAY_TIMEZONE=Asia/Almaty      # таймзона колонок «Дата добавления»/«Дата последнего контакта»
SHEET_PUBLIC_URL=...              # ссылка на таблицу: /settings и сообщение «Лид добавлен — ID #N, строка M»
DUP_NOTIFICATIONS_ENABLED=true    # что показывает /settings в строке «Уведомления о дублях»
DEDUP_CANDIDATE_LIMIT=500         # максимум кандидатов на одну проверку дублей
LOG_LLM_BODIES=true               # хранить тела ответов LLM в БД (в логи они не попадают)
EXTRACTION_LOG_RETENTION_DAYS=30  # сколько дней хранить extraction_logs; 0 — бессрочно
EXTRACTION_LOG_CLEANUP_INTERVAL_SECONDS=86400  # период очистки; 0 — выключить воркер
SEARCH_RESULT_LIMIT=20            # сколько результатов максимум отдаёт /search
LAST_LEADS_LIMIT=5                # сколько лидов показывает /last
```

### 1. Создание venv и установка

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows (Git Bash: source .venv/Scripts/activate)
pip install -r requirements-dev.txt
```

### 2. Ключ OpenRouter

1. Зарегистрируйтесь на https://openrouter.ai
2. https://openrouter.ai/keys → Create Key → скопируйте в `OPENROUTER_API_KEY`.

### 2а. Модели LLM

Модели задаются переменными окружения (пустое значение = дефолт из кода):

- `OPENROUTER_MODEL` — основная модель. По умолчанию `openrouter/free`.
- `OPENROUTER_FALLBACK_MODEL` — запасная модель. По умолчанию `nvidia/nemotron-3-ultra-550b-a55b:free`.

Обе дефолтные модели — бесплатные (`:free`), проверены живыми вызовами OpenRouter с реальным
системным промптом бота: полный цикл извлечения проходит, `usage.cost = 0`. В
`extraction_logs.cost_usd_est` для `:free`-моделей всегда пишется `0.0`.

**Лимиты free-tier OpenRouter.** При балансе аккаунта `$0` на бесплатные модели действует лимит
**50 запросов/день**. После пополнения баланса на **$10** лимит на бесплатные модели вырастает до
**1000 запросов/день**. Платные модели (например, `openai/gpt-4o-mini`) при нулевом балансе
считать надёжными нельзя — без пополнения они не пройдут оплату и могут молча не работать, хотя
каталог и возвращает 200 на запрос списка моделей.

Чтобы сменить модель, укажите её slug из каталога OpenRouter:

```env
OPENROUTER_MODEL=nex-agi/nex-n2.5-pro:free
OPENROUTER_FALLBACK_MODEL=liquid/lfm-2.5-2.6b:free
```

> ⚠️ Не используйте `openai/gpt-4o-mini` и другие платные модели без пополнения баланса —
> на free-tier аккаунте с `$0` они ненадёжны.


### 3. Путь A — сервис-аккаунт Google (нужна карта для GCP)

1. Откройте https://console.cloud.google.com → создайте проект → включите **Google Sheets API**.
2. **IAM и администрирование → Учётные записи сервисов** → Создать → роль «Редактор» не нужна,
   достаточно **доступа только к таблице**.
3. В карточке сервис-аккаунта: **Ключи → Добавить ключ → JSON** — скачается JSON-файл.
4. Закодируйте его в одну строку base64:

   ```bash
   python -c "import base64,sys; print(base64.b64encode(open('ключ.json','rb').read()).decode())"
   ```

   Полученную строку вставьте в `GOOGLE_SERVICE_ACCOUNT_JSON`.
5. Откройте целевую Google-таблицу → **Настройки доступа** → выдайте доступ на **email сервис-аккаунта**
   (вида `...@...iam.gserviceaccount.com`) с правами «Редактор».
6. Создайте в таблице лист с именем **Leads** (первая строка — заголовки). Порядок колонок фиксирован (A–AA).

### 3а. Путь B — Apps Script Web App (без GCP и банковской карты)

Если привязать карту к `console.cloud.google.com` нельзя, сервис-аккаунт недоступен. Вместо него
используется **Google Apps Script Web App**, привязанный прямо к самой таблице — бесплатно, без GCP.

1. Откройте целевую таблицу → меню **Расширения → Apps Script**.
2. Вставьте код целиком из [`docs/google_apps_script_webhook.gs`](docs/google_apps_script_webhook.gs).
3. Задайте константу `TOKEN` в начале скрипта — любой случайный секрет.
4. **Деплой → Новое развертывание → тип «Веб-приложение»**:
   - «Выполнять как»: **я**
   - «Доступ»: **все, у кого есть ссылка**
5. Скопируйте URL вида `https://script.google.com/macros/s/…/exec` в `GOOGLE_SHEETS_WEBHOOK_URL`,
   а значение `TOKEN` — в `GOOGLE_SHEETS_WEBHOOK_TOKEN`.

**Если проект создан отдельно от таблицы (standalone).** Шаги 1–5 выше описывают контейнерный
скрипт, созданный из самой таблицы. Если вместо этого вы зашли на `script.google.com` и создали
проект там, активной таблицы у скрипта нет — задайте в начале скрипта константу
`SPREADSHEET_ID`: возьмите её из адреса таблицы (часть между `/d/` и `/edit`, например
`https://docs.google.com/spreadsheets/d/1AbCdEf1234567890/edit#gid=0` → `1AbCdEf1234567890`).
Остальные шаги (деплой Web App и перенос URL/токена в `.env`) те же.

**Проверка деплоя.** Откройте URL `/exec` прямо в браузере — это GET-запрос, и скрипт должен
вернуть `{"ok":true,"service":"leadforge-webhook"}`. Такой ответ означает, что развёртывание
работает. Если браузер просит войти в Google — доступ выставлен не «все, у кого есть ссылка».
Если вернулось `{"ok":false,...}` — деплой жив, но скрипт не видит таблицу: проверьте
`SPREADSHEET_ID` и имя листа `SHEET_NAME`.

Выбор бэкенда автоматический: задан `GOOGLE_SERVICE_ACCOUNT_JSON` → gspread (путь A); иначе задан
`GOOGLE_SHEETS_WEBHOOK_URL` → webhook (путь B); иначе синхронизация «не настроена» — лид сохраняется
в SQLite с `sheet_row=NULL` и досинхронизируется автоматически (фоновый воркер, период
`AUTO_RESYNC_INTERVAL_SECONDS`) либо командой `/resync`.

**Безопасность.** `GOOGLE_SHEETS_WEBHOOK_URL` + `GOOGLE_SHEETS_WEBHOOK_TOKEN` — это секрет: кто знает
оба значения, тот может писать в таблицу. Редактирует таблицу сам скрипт от имени владельца
(«Выполнять как: я»), поэтому доступ посторонним не выдаётся, а токен — единственный ключ.

**Квоты.** Для личного использования (десятки лидов/день) запас Apps Script огромный; отдельного
лимита на запись в свою таблицу через Web App для такого объёма не ощущается.

> **Редирект `/exec` (важно).** Apps Script Web App отвечает на POST запросом HTTP **302**
> с пустым телом и заголовком `Location: https://script.googleusercontent.com/macros/echo?...`;
> сам JSON отдаётся только по адресу из `Location`. `Content-Type` этого не меняет: `text/plain`
> безвреден (скрипт читает `e.postData.contents`), но клиент **обязан следовать редиректу**.
> Поэтому python-клиент создаёт `httpx.AsyncClient(..., follow_redirects=True)`. Если редирект
> не пройден, тело ответа пустое, попытка падает и повторяется — раньше это давало дубли строк.

**Идемпотентность append.** `doPost` с `action=append` перед добавлением ищет `values[0]` (ID лида)
в колонке A целевого листа (строка заголовков исключена, поиск идёт по диапазону колонки, а не по
всему листу). Если строка с таким ID уже есть — новая не добавляется, возвращается
`{"ok":true,"row":<номер существующей строки>,"duplicate":true}`. Пустой `values[0]` — добавляем
без проверки. Так повторный POST после сетевого ретрая не создаёт дубликат. То же правило
реализовано на стороне gspread-пути (`SheetsSyncService._append`): перед `append_row` проверяется
колонка A, и при повторе возвращается номер существующей строки.

**Проверка строки при update.** `lead.sheet_row` — это кэш *номера* строки, а не доказательство
владения: если в таблице вручную вставить или удалить строку, все номера ниже сдвигаются, и запись
«по голому номеру» перезаписывает чужого лида. Поэтому:

- python-клиент передаёт в `update` дополнительное поле `lead_id` (для `/undo` — ID лида, чью
  строку очищаем);
- скрипт перед записью сверяет `A{row}` с `lead_id`: совпало — пишет в `row`; не совпало — ищет
  строку по ID (`findRowById`) и пишет в неё; не находит — **не пишет ничего** и отвечает
  `{"ok":false,"error":"row not found for id ..."}` (клиент считает синхронизацию неуспешной и
  повторит её позже — лид остаётся в SQLite и попадает в очередь досинхронизации);
- в ответе скрипт всегда возвращает фактический номер строки: `{"ok":true,"row":N}`. Если он
  отличается от кэшированного, клиент обновляет `sheet_row` в БД — кэш самовосстанавливается;
- повтор потерянного ответа на очистку строки (строка уже пуста, лида в таблице нет) — это
  `{"ok":true,"row":N,"blank":true}`, а не ошибка;
- совместимость: если `lead_id` не передан (старый клиент), скрипт пишет по `row`, как раньше; если
  развёрнут старый скрипт (не понимает `lead_id`), он игнорирует лишнее поле и отвечает без `row` —
  клиент в этом случае доверяет кэшу. Обновление Apps Script без смены URL — см. раздел
  «Обновление Apps Script без смены URL».


### 4. Как узнать свой telegram_id

1. Запустите бота с пустым `ALLOWED_USER_IDS` и напишите ему что-нибудь.
2. Бот ответит «Это приватный бот.», а в логах появится строка уровня WARNING с пометкой
   `allowlist_empty` и вашим `telegram_user_id`.
3. Впишите этот id в `ALLOWED_USER_IDS` и перезапустите бота.

Либо используйте любого бота-«id-резолвера» или метод `getUpdates`.

**Второй источник доступа — таблица `users`.** Доступ разрешён, если id есть в
`ALLOWED_USER_IDS` **или** в таблице `users` (поле `telegram_user_id`) — добавить
пользователя можно без правки переменных окружения и редеплоя:

```sql
INSERT INTO users (telegram_user_id, display_name, role) VALUES (123456789, 'Коллега', 'owner');
```

Источник, по которому прошёл доступ, пишется в лог: `allowlist_allowed` с полем `source`
(`env` или `users_table`), по одному разу на пользователя за процесс. Если id нет ни там,
ни там — прежний нейтральный ответ «Это приватный бот.» и запись `allowlist_blocked` (или
`allowlist_empty`, если `ALLOWED_USER_IDS` пуст). Ошибка чтения таблицы доступ **не** выдаёт:
она логируется, а решение остаётся за env-списком.

### 5. Миграции БД

```bash
.venv/Scripts/python -m alembic upgrade head
```

При запуске приложения миграции применяются автоматически (идемпотентно).

## Запуск

### Dev-режим (long polling, Windows без публичного URL)

```bash
DEV_POLLING=true .venv/Scripts/python -m app.main
```

PowerShell-вариант: в `.env` поставьте `DEV_POLLING=true`, затем:

```powershell
.\.venv\Scripts\python.exe -m app.main
```

В этом режиме бот работает через `getUpdates` (polling) — это единственное разрешённое отступление от
требования «только webhook», нужно для локального тестирования.

### Production-режим (webhook)

```bash
.venv/Scripts/python -m app.main
```

Запускается uvicorn на `0.0.0.0:$PORT` (по умолчанию 8080); в lifespan выполняются
миграции и регистрация webhook (`WEBHOOK_URL`, а в Railway — из `RAILWAY_PUBLIC_DOMAIN`;
подробности — «Продакшн-деплой на Railway»).

Эндпоинты:
- `GET /health` — health-check
- `POST /webhook` — приём обновлений Telegram (проверяется `X-Telegram-Bot-Api-Secret-Token`, если задан `WEBHOOK_SECRET`)

  Тело читается и разбирается внутри хендлера (`await request.body()` + `json.loads`), а не
  параметром роута: аннотация параметра без `Body(...)` (например `update: Any`) становится
  **обязательным query-параметром**, и тогда каждый реальный POST падает на валидации до входа
  в хендлер — наружу при этом уходит 200, и бот молча игнорирует всё (именно так и случилось в
  проде). Коды ответов: `200` — обработано либо тело не распознано (лог `webhook_bad_body` /
  `webhook_bad_payload`), `403` — неверный секрет, `503` — контейнер не инициализирован,
  `500` — настоящая ошибка обработки. Регрессию стережёт `tests/test_webhook_asgi.py`
  (реальный ASGI-запрос на `/webhook`, а не самодельный `Request`).

### Ручная установка webhook

```bash
.venv/Scripts/python - <<'PY'
import asyncio
from aiogram import Bot
from app.config import get_settings

s = get_settings()
async def main():
    bot = Bot(s.BOT_TOKEN)
    await bot.set_webhook(url=s.WEBHOOK_URL, secret_token=s.WEBHOOK_SECRET or None, drop_pending_updates=True)
    print((await bot.get_webhook_info()).model_dump())
    await bot.session.close()

asyncio.run(main())
PY
```

## Продакшн-деплой на Railway

Сервис собирается из `Dockerfile` (база `python:3.11-slim`) по конфигу `railway.toml`:
builder — Dockerfile, healthcheck — `GET /health`, `numReplicas = 1` (одновременная
обработка вебхука несколькими копиями и один файл SQLite несовместимы).

```bash
railway up            # или подключите GitHub-репозиторий к сервису
```

### Переменные окружения

Задаются в Railway → Service → Variables. Имена — те же, что и в `.env`.

| Переменная | Обяз. | Назначение |
|---|---|---|
| `BOT_TOKEN` | да | токен бота у @BotFather |
| `ALLOWED_USER_IDS` | нет † | telegram id через запятую; можно не задавать, если id добавлен в таблицу `users` |
| `OPENROUTER_API_KEY` | да | ключ https://openrouter.ai/keys |
| `OPENROUTER_MODEL` | нет | по умолчанию `openrouter/free` |
| `OPENROUTER_FALLBACK_MODEL` | нет | запасная модель |
| `GOOGLE_SHEETS_WEBHOOK_URL` | да* | `/exec` Apps Script Web App (путь B) |
| `GOOGLE_SHEETS_WEBHOOK_TOKEN` | да* | токен из скрипта Apps Script (путь B) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | да* | base64 сервис-аккаунта (путь A) |
| `GOOGLE_SHEET_ID` | да* | id таблицы (путь A) |
| `DATABASE_URL` | да | см. «Эфемерная ФС» ниже |
| `DEV_POLLING` | да | **`false`** в проде |
| `WEBHOOK_SECRET` | рекоменд. | секрет для заголовка `X-Telegram-Bot-Api-Secret-Token` |
| `WEBHOOK_URL` | нет | если пусто — соберётся из `RAILWAY_PUBLIC_DOMAIN` |
| `DEFAULT_CITY` | нет | по умолчанию `Алматы` |
| `COLLECT_TIMEOUT_SECONDS` | нет | по умолчанию `7` |
| `AUTO_RESYNC_INTERVAL_SECONDS` | нет | период авто-досинхронизации лидов без строки в Sheets, по умолчанию `300` (5 минут); `0` — выключить |
| `DISPLAY_TIMEZONE` | нет | таймзона для колонок «Дата добавления» и «Дата последнего контакта», по умолчанию `Asia/Almaty`; в БД время остаётся UTC |
| `SHEET_PUBLIC_URL` | нет | публичная ссылка на таблицу: показывается в `/settings` и в сообщении после добавления; пусто — ссылки нет (из `GOOGLE_SHEET_ID` собирается только при его наличии) |
| `DUP_NOTIFICATIONS_ENABLED` | нет | состояние, которое показывает `/settings` в строке «Уведомления о дублях», по умолчанию `true` |
| `DEDUP_CANDIDATE_LIMIT` | нет | сколько кандидатов максимум поднимает одна проверка дублей, по умолчанию `500`; при достижении лимита в лог пишется предупреждение |
| `SEARCH_RESULT_LIMIT` | нет | сколько лидов максимум возвращает `/search` (сначала новые), по умолчанию `20` |
| `LAST_LEADS_LIMIT` | нет | сколько лидов показывает `/last`, по умолчанию `5` |
| `LOG_LLM_BODIES` | нет | хранить полные тела ответов LLM в `extraction_logs.response_body` (обрезанные до 4000 символов, только в БД, никогда в stdout-логи), по умолчанию `true`; `false` — не хранить |
| `EXTRACTION_LOG_RETENTION_DAYS` | нет | сколько дней хранить `extraction_logs`, по умолчанию `30`; `0` — хранить бессрочно (очистка выключается) |
| `EXTRACTION_LOG_CLEANUP_INTERVAL_SECONDS` | нет | период фоновой очистки `extraction_logs`, по умолчанию `86400` (раз в сутки); `0` — выключить |
| `LOG_FILE` | нет | файловый лог (JSON, с ротацией), по умолчанию `/data/logs/leadforge.log`; `off` — только stdout |
| `LOG_MAX_BYTES` | нет | порог ротации, по умолчанию `5000000` |
| `LOG_BACKUP_COUNT` | нет | число файлов ротации, по умолчанию `3` |

\* нужен либо `GOOGLE_SHEETS_WEBHOOK_URL` + `GOOGLE_SHEETS_WEBHOOK_TOKEN` (путь B),
либо `GOOGLE_SERVICE_ACCOUNT_JSON` + `GOOGLE_SHEET_ID` (путь A).

† доступ разрешён, если id есть в `ALLOWED_USER_IDS` **или** в таблице `users`:
с пустым env-списком ботом можно пользоваться, добавив строку в таблицу (см. «Как узнать
свой telegram_id»). Если оба источника пусты, бот отвечает «Это приватный бот.», а id
попадает в лог — его можно вписать любым из двух способов.

Служебные переменные Railway приложению задавать не нужно — оно читает их само:

- `PORT` — порт uvicorn (`app.main.resolve_port()`, по умолчанию 8080).
- `RAILWAY_PUBLIC_DOMAIN` — публичный домен сервиса (Settings → Networking →
  Generate Domain), включается кнопкой; из него строится `WEBHOOK_URL`.

### Регистрация вебхука при старте

`lifespan` → `startup_runtime()`:

1. применяются миграции Alembic (идемпотентно, `alembic upgrade head`);
2. закрываются сессии, оставшиеся от прошлого процесса: диалог живёт в памяти
   (`MemoryStorage` + буфер), поэтому после рестарта строки в `lead_sessions` со статусом
   `collecting`/`review`/`editing` завершить уже нельзя — они помечаются `cancelled`, и в лог
   пишется `sessions_reconciled` с количеством и id (набранный текст при этом сохраняется);
3. запускаются фоновые воркеры (оба живут в реестре задач контейнера и отменяются при
   shutdown, падение прохода не роняет процесс):
   - авто-досинхронизации (`AUTO_RESYNC_INTERVAL_SECONDS`) — если Google Sheets настроен;
   - очистки `extraction_logs` (`EXTRACTION_LOG_CLEANUP_INTERVAL_SECONDS`,
     `EXTRACTION_LOG_RETENTION_DAYS`) — удаляет записи старше окна хранения, логируя
     `extraction_log_cleanup` с их количеством; лидов, сессий и `audit_log` не касается.
     Первый проход выполняется сразу при старте, дальше — раз в интервал: контейнер
     перезапускается чаще, чем раз в сутки, поэтому «ждать 24 часа» не удалило бы ничего;
4. если `DEV_POLLING=false`:
   - `WEBHOOK_URL` задан → используется он;
   - `WEBHOOK_URL` пуст, но есть `RAILWAY_PUBLIC_DOMAIN` → берётся
     `https://<RAILWAY_PUBLIC_DOMAIN>/webhook`;
   - ни то, ни другое → warning, вебхук не регистрируется;
5. результат регистрации (успех или ошибка) пишется в лог; при ошибке контейнер не
   падает — `/health` продолжает отвечать, а причина видна в логах.

При остановке (`Container.close()`) сначала дожидаются фоновые задачи в полёте (синхронизация
последнего лида и её уведомление), затем отменяются периодические воркеры, и только потом
закрываются http-клиенты и движок БД — иначе задача осталась бы без соединений.

### Как убедиться, что активен именно вебхук

1. В логе старта есть строки `registering webhook at https://<домен>/webhook
   (source: RAILWAY_PUBLIC_DOMAIN)` и `webhook registration OK: ...`.
2. Спросить Telegram напрямую:

   ```bash
   curl -s "https://api.telegram.org/bot<BOT_TOKEN>/getWebhookInfo"
   ```

   Ожидаем `"url": "https://<домен>/webhook"` и `pending_update_count` около нуля.
3. Локальный polling остановлен: в проде `DEV_POLLING=false`, поэтому `run_polling()`
   не запускается, и процесс обслуживает только uvicorn. Если локальный бот всё ещё
   крутится с polling, Telegram отдаёт `Conflict: terminated by other getUpdates`,
   а вебхук не получает обновления — выключите локальный процесс.
4. `curl -s https://<домен>/health` → `{"status":"ok"}`.

### Логи

```bash
railway logs        # поток логов запущенного сервиса
```

Логи — JSON-строки (см. `app/logging_config.py`): по ним проверяются
`Alembic migrations applied` и `webhook registration OK/FAILED`.
Примечание: миграции запускаются тем же процессом, что и приложение, поэтому важно,
чтобы Alembic не отключал уже созданные логгеры (`disable_existing_loggers=False`
в `alembic/env.py`) — иначе логи после старта пропадают.

### Обновление Apps Script без смены URL

URL `/exec` привязан к **deployment ID**, а не к версии кода. Чтобы обновить скрипт,
сохранив тот же `GOOGLE_SHEETS_WEBHOOK_URL`:

1. Открыть таблицу → Extensions → Apps Script.
2. Deploy → **Manage deployments**.
3. Выбрать активный deployment → карандаш (Edit) → Version: **New version** → Deploy.

URL `/exec` не меняется, править переменные в Railway не нужно. Если вместо этого
сделать «New deployment», получится **новый** URL — тогда обновите
`GOOGLE_SHEETS_WEBHOOK_URL` и передеплойте сервис.

Это же нужно сделать один раз, чтобы включилась проверка строки при `update`
(поле `lead_id`, см. «Проверка строки при update»): пока развёрнута старая версия
скрипта, бот работает как раньше — пишет по кэшированному номеру строки. Чтобы
убедиться, что новая версия развёрнута, можно отправить `update` вручную и посмотреть
ответ: новая версия возвращает `{"ok":true,"row":N}`, старая — `{"ok":true}`.

### ⚠️ Эфемерная файловая система Railway — риск для SQLite

По умолчанию ФС контейнера **эфемерна**: всё, что записано вне Volume, исчезает при
каждом редеплое/рестарте. `DATABASE_URL` по умолчанию указывает на `leadforge.db`
внутри контейнера, значит **после редеплоя пропадут лиды, сессии, `sheet_row`
и `extraction_logs`**; строки в Google Sheets останутся, но `/undo`, `/resync` и
дедупликация потеряют состояние (дедуп сравнивает с локальной БД).

Варианты:

**A. Railway Volume (быстро, для небольших объёмов).**

1. Service → Settings → Volumes → New Volume, mount path — например `/data`.
2. Переменная `DATABASE_URL=sqlite+aiosqlite:////data/leadforge.db`
   (четыре слэша — это абсолютный путь `/data/leadforge.db`).
3. Redeploy: `alembic upgrade head` выполнится при старте, БД на Volume переживёт
   редеплои. `numReplicas` должен остаться `1` — два контейнера на одном файле SQLite
   приведут к блокировкам.

Логи (`LOG_FILE`, по умолчанию `/data/logs/leadforge.log`) тоже пишутся на Volume —
без Volume они живут только в stdout контейнера и исчезают при редеплое. Если каталог
недоступен, приложение стартует и продолжает писать только в stdout.

**B. Postgres (рекомендуется при росте данных).**
Модель данных уже готова к этому: `owner_user_id` в `leads`, таблицы `users` и
`audit_log`. Подключите Railway Postgres и задайте
`DATABASE_URL=postgresql+asyncpg://<user>:<pass>@<host>:<port>/<db>`.
Драйвера `asyncpg` в `requirements.txt` пока нет — добавьте его. Обратите внимание:
готовой нормализации `postgres://` → `postgresql+asyncpg://` в конфиге нет, URL нужно
записать сразу в форме `postgresql+asyncpg://`. SQLite-специфичные PRAGMA
(`app/database.py`) применяются только когда URL начинается с `sqlite`, поэтому
Postgres заработает без правок кода.

**C. Осознанный риск.** Оставить SQLite в контейнере и принять, что история лидов
живёт до следующего деплоя (Google Sheets при этом данные сохраняет). Годится только
для пилота — при любом редеплое локальное состояние обнуляется.

## Команды

| Команда | Назначение |
|---|---|
| `/start` | приветствие и инструкция |
| `/new` | начать новый лид (не сливая с текущим буфером) |
| `/done` | финализировать буфер сразу |
| `/cancel` | отменить текущий сбор |
| `/last` | последние добавленные лиды (`LAST_LEADS_LIMIT`, по умолчанию 5) |
| `/search <запрос>` | поиск по имени/телефону/Instagram/сайту: не более `SEARCH_RESULT_LIMIT` результатов (по умолчанию 20), сначала самые новые |
| `/stats` | лиды (всего/за неделю), объединённые дубли, обращения к AI (успешные/всего), токены (вход/выход) и расход на AI: сумма из `extraction_logs.cost_usd_est` (`usage.cost` провайдера, иначе — тариф модели); для бесплатной модели пишется «0 (бесплатная модель)», а не выдуманная сумма |
| `/undo` | откатить последнее действие (создание/merge); можно подряд, откат отражается в таблице. Откат слияния восстанавливает целевой лид из снапшота `audit_log` и перезаписывает его строку, а слитый дубль остаётся архивным (второй строки в таблице не появляется) |
| `/settings` | настройки (город, уведомления о дублях, ссылка на таблицу) |
| `/resync` | досинхронизировать лиды, оставшиеся без строки в Sheets (вручную; то же самое автоматически делает фоновый воркер) |
| `/help` | список команд |

## Тесты

```bash
.venv/Scripts/python -m pytest -q
```

Покрытие:
- нормализация телефонов / сайтов / instagram (грязные форматы)
- парсинг и валидация JSON-ответа LLM, невалидный JSON → retry, fallback
- оба уровня дедупликации
- FSM-переходы (буферизация, `/done` раньше таймера, `/cancel`, ручной ввод)
- запись/обновление строки в Sheets через мок gspread
- webhook-бэкенд Sheets (Apps Script): append → кэш `sheet_row`, update по кэшу, ретрай на 500, неверный токен, следование 302-редиректу `/exec` (httpx.MockTransport), выбор бэкенда по конфигу
- целостность строк таблицы: сдвиг строки вручную → запись уходит в строку, где реально лежит ID лида,
  а если лида в колонке A нет — записи не происходит вовсе (оба бэкенда, включая отказ вместо записи);
  идемпотентный append (повтор после потерянного ответа не создаёт вторую строку); деградация к прежнему
  поведению со старым скриптом без `lead_id`
- Apps Script как таковой: `docs/google_apps_script_webhook.gs` исполняется в Node VM с подставным
  `SpreadsheetApp` (`tests/apps_script_harness.js`) — проверяются ветки update/append на сдвиг строки,
  отсутствие лида, повтор очистки; модуль пропускается, если Node не установлен
- allowlist: чужой id не получает доступа; доступ по строке в таблице `users`, доступ по env,
  отказ при отсутствии в обоих источниках, источник доступа в логе, ошибка чтения таблицы доступ не выдаёт
- merge-логика и `/undo` (в т.ч. откат слияния: дубль остаётся архивным, целевой лид отражает
  снятое слияние, лишняя строка в таблицу не добавляется даже после `/resync`)
- закрытие «зависших» сессий на старте (collecting/review/editing → cancelled, с логом)
  и сам набор статусов сессии (`collecting → review → editing → review → done`/`cancelled`)
- фоновые задачи: реестр держит ссылки, дожидается задач в полёте при shutdown, отменяет воркеры;
  авто-досинхронизация очереди по всем владельцам, переживает падение одного лида и целого прохода
- тела ответов LLM (ТЗ §11): обрезка до 4000 символов, `LOG_LLM_BODIES=false` не пишет ничего,
  тело не попадает в логи (только его длина), очистка `extraction_logs` старше окна хранения
  и её воркер (включая падение прохода и отмену при shutdown)
- подтверждение после добавления — одно сообщение (`ID #N, строка M` + ссылка), в т.ч. когда
  запись в таблицу отложена; лимиты `/last` и `/search` берутся из окружения

## Примечания по поведению

- LLM не выдумывает данные — отсутствующие поля `null`; неуверенные поля попадают в `uncertain_fields` (⚠️ на карточке).
- Сильное совпадение (телефон/сайт/Instagram/Telegram) — авто-merge без вопросов; среднее — явный выбор кнопками.
- После добавления лида бот отправляет **одно** сообщение: «✅ Лид добавлен — ID #N, строка M» (и ссылку на таблицу, если задан `SHEET_PUBLIC_URL`). Номер строки известен только после записи в Sheets, поэтому сообщение отправляет фоновая задача синхронизации: пока она ждёт таблицу, диалог не блокируется. Если запись не удалась — то же одно сообщение, но с «синхронизация чуть задержится, позже выполните /resync».
- Статусы `lead_sessions`: `collecting` (собирается текст) → `review` (открыт диалог: карточка, вопрос о дубле, ручной ввод) → `editing` (открыт выбор поля «Исправить») → снова `review` после правки → `done` (лид сохранён) либо `cancelled` (отмена/`/start`/рестарт). Статус `editing` пишется при нажатии «Исправить», поэтому рестарт закрывает и такие сессии.
- Ошибка Google Sheets API не теряет лид: он уже в SQLite, синхронизация ретраится в фоне; при неудаче `sheet_row=NULL`, лид попадает в очередь и досинхронизируется автоматически (воркер, период `AUTO_RESYNC_INTERVAL_SECONDS`) либо командой `/resync`.
- Строка в таблице считается своей только если в колонке A стоит ID этого лида: кэш `sheet_row` не даёт права писать в строку, которая после ручной правки таблицы принадлежит другому лиду. Если лид в таблице не найден, запись не выполняется (лид остаётся в БД и в очереди), а не «пишется куда-нибудь».
- Значения, начинающиеся с `= + - @`, экранируются ведущим апострофом (защита от formula injection).
