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
    lead_service.py    # бизнес-логика лидов (создание, merge, undo, stats)
    session_buffer.py  # буфер сообщений с отменяемым таймером
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
DEFAULT_CITY=Алматы
COLLECT_TIMEOUT_SECONDS=7
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

Выбор бэкенда автоматический: задан `GOOGLE_SERVICE_ACCOUNT_JSON` → gspread (путь A); иначе задан
`GOOGLE_SHEETS_WEBHOOK_URL` → webhook (путь B); иначе синхронизация «не настроена» — лид сохраняется
в SQLite с `sheet_row=NULL` и досинхронизируется командой `/resync`.

**Безопасность.** `GOOGLE_SHEETS_WEBHOOK_URL` + `GOOGLE_SHEETS_WEBHOOK_TOKEN` — это секрет: кто знает
оба значения, тот может писать в таблицу. Редактирует таблицу сам скрипт от имени владельца
(«Выполнять как: я»), поэтому доступ посторонним не выдаётся, а токен — единственный ключ.

**Квоты.** Для личного использования (десятки лидов/день) запас Apps Script огромный; отдельного
лимита на запись в свою таблицу через Web App для такого объёма не ощущается.

> Примечание: python-клиент шлёт POST с `Content-Type: text/plain` (JSON-строкой), потому что на
> `application/json` endpoint `/exec` отвечает 302-редиректом на `script.googleusercontent.com`.


### 4. Как узнать свой telegram_id

1. Запустите бота с пустым `ALLOWED_USER_IDS` и напишите ему что-нибудь.
2. Бот ответит «Это приватный бот.», а в логах появится строка уровня WARNING с пометкой
   `allowlist_empty` и вашим `telegram_user_id`.
3. Впишите этот id в `ALLOWED_USER_IDS` и перезапустите бота.

Либо используйте любого бота-«id-резолвера» или метод `getUpdates`.

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

Запускается uvicorn на `0.0.0.0:8080`; в lifespan выполняется регистрация webhook (нужен `WEBHOOK_URL`).

Эндпоинты:
- `GET /health` — health-check
- `POST /webhook` — приём обновлений Telegram (проверяется `X-Telegram-Bot-Api-Secret-Token`, если задан `WEBHOOK_SECRET`)

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

## Команды

| Команда | Назначение |
|---|---|
| `/start` | приветствие и инструкция |
| `/new` | начать новый лид (не сливая с текущим буфером) |
| `/done` | финализировать буфер сразу |
| `/cancel` | отменить текущий сбор |
| `/last` | последние добавленные лиды |
| `/search <запрос>` | поиск по имени/телефону/Instagram/сайту |
| `/stats` | лиды (всего/за неделю), дубли, расход на AI |
| `/undo` | откатить последнее действие (создание/merge) |
| `/settings` | настройки (город, уведомления о дублях, ссылка на таблицу) |
| `/resync` | досинхронизировать лиды, оставшиеся без строки в Sheets |
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
- webhook-бэкенд Sheets (Apps Script): append → кэш `sheet_row`, update по кэшу, ретрай на 500, неверный токен, выбор бэкенда по конфигу
- allowlist: чужой id не получает доступа
- merge-логика и `/undo`

## Примечания по поведению

- LLM не выдумывает данные — отсутствующие поля `null`; неуверенные поля попадают в `uncertain_fields` (⚠️ на карточке).
- Сильное совпадение (телефон/сайт/Instagram/Telegram) — авто-merge без вопросов; среднее — явный выбор кнопками.
- Ошибка Google Sheets API не теряет лид: он уже в SQLite, синхронизация ретраится в фоне; при неудаче `sheet_row=NULL` и `/resync`.
- Значения, начинающиеся с `= + - @`, экранируются ведущим апострофом (защита от formula injection).
