"""Integration harness: drive synthetic Telegram updates through the *real* dispatcher.

Production keeps its object graph on ``app.di.Container``. These helpers build the
real container on a throwaway SQLite file (schema created by Alembic, not
``create_all``) and then swap in recording fakes for the only three collaborators
that would otherwise leave the process:

* ``bot``        → Telegram API,
* ``extraction`` → OpenRouter,
* ``sheets``     → Google Sheets.

Everything else is the production code path: ``Dispatcher``, routers, the
allowlist/retry middlewares, FSM storage, ``LeadService``, ``DedupService`` and
``SessionBufferService``.
"""
from __future__ import annotations

import asyncio
import importlib
import re
import socket
import xml.etree.ElementTree as ElementTree
from contextlib import suppress
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram.types import Update
from sqlalchemy import select

import app.bot.dispatcher as dispatcher_module
from app.config import get_settings
from app.database import run_migrations_async
from app.di import Container
from app.models import Lead, LeadSession

# A real-looking token; aiogram validates "<digits>:<non-empty>".
TEST_BOT_TOKEN = "123456:TEST-TOKEN-FOR-INTEGRATION-TESTS"

OWNER_USER_ID = 424242
STRANGER_USER_ID = 999999

BASE_DATE = 1_700_000_000

# A premium (custom) emoji looks like <tg-emoji emoji-id="123">✅</tg-emoji>.
TG_EMOJI_RE = re.compile(r'<tg-emoji emoji-id="(\d+)">(.*?)</tg-emoji>')

# Ranges that cover every plain emoji the bot used before the premium ones
# (✅ U+2705, ❌ U+274C, ✏ U+270F, ⏰ U+23F0, 👋/📊/… U+1F000+). Deliberately
# excludes U+2000–U+206F so «» — … and → in Russian copy are not false positives.
PLAIN_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2300-\u23FF\uFE0F]"
)


def iter_buttons(reply_markup):
    """Flatten an inline (or reply) markup into its buttons."""
    if reply_markup is None:
        return []
    rows = getattr(reply_markup, "inline_keyboard", None)
    if rows is None:
        rows = getattr(reply_markup, "keyboard", None) or []
    return [button for row in rows for button in row]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail loudly if any test path tries to talk to the outside world.

    Import this fixture (``from tests.integration_harness import no_network``) in
    every integration test module — it is autouse, so importing it is enough.

    Loopback is allowed because the Windows event loop builds its self-pipe via
    ``socket.socketpair()`` (a loopback TCP connection); everything else raises.
    """
    real_connect = socket.socket.connect
    loopback = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}

    def guarded_connect(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if host in loopback:
            return real_connect(self, address, *args, **kwargs)
        raise AssertionError(f"network connection attempted: {address!r}")

    def blocked_create_connection(*args, **kwargs):
        raise AssertionError("network access attempted via socket.create_connection")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "create_connection", blocked_create_connection)


def has_plain_emoji(text: str | None) -> bool:
    return bool(text) and bool(PLAIN_EMOJI_RE.search(text))


def find_plain_emoji(text: str) -> list[str]:
    """Plain emoji left *outside* premium tags — i.e. a missed replacement."""
    return PLAIN_EMOJI_RE.findall(TG_EMOJI_RE.sub("", text))


def find_raw_angles(text: str) -> list[str]:
    """``<``/``>`` left over once premium-emoji tags are removed.

    Any leftover angle bracket means user data reached an HTML message
    unescaped — i.e. Telegram would answer 400 "can't parse entities".
    """
    return [char for char in TG_EMOJI_RE.sub("", text) if char in "<>"]


def assert_valid_telegram_html(text: str) -> None:
    """Assert *text* survives Telegram's HTML parser untouched.

    Checks the two ways an HTML message breaks in production: a raw ``<``/``>``
    from unescaped user data, and a bare ``&`` that is not a valid entity (both
    make ElementTree fail exactly like Telegram's parser does).
    """
    raw = find_raw_angles(text)
    assert not raw, f"raw angle brackets in HTML message: {text!r}"
    leftovers = find_plain_emoji(text)
    assert not leftovers, f"plain emoji outside a premium tag: {leftovers!r} in {text!r}"
    try:
        ElementTree.fromstring(f"<root>{text}</root>")
    except ElementTree.ParseError as exc:  # pragma: no cover - only on a regression
        raise AssertionError(f"not valid Telegram HTML ({exc}): {text!r}") from exc


@dataclass
class OutgoingCall:
    """One recorded outgoing Telegram call."""

    method: str
    chat_id: int | None = None
    text: str | None = None
    reply_markup: object | None = None
    callback_query_id: str | None = None
    payload: object | None = None
    parse_mode: object | None = None


class RecordingBot:
    """Fake ``Bot``: records every outgoing call instead of hitting Telegram.

    Both call styles are supported, because the code under test uses both:

    * direct shortcuts — ``await container.bot.send_message(...)``;
    * aiogram shortcuts on updates — ``await message.answer(...)``, which builds a
      ``SendMessage`` method object and awaits it, landing in ``__call__``.
    """

    def __init__(self, bot_id: int = 1) -> None:
        self.id = bot_id
        self.calls: list[OutgoingCall] = []
        self._message_id = 0

    # ---- direct API ----
    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None, **kwargs):
        self._message_id += 1
        self.calls.append(
            OutgoingCall(
                "send_message",
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
        )
        return SimpleNamespace(message_id=self._message_id)

    async def answer_callback_query(self, callback_query_id, text=None, **kwargs):
        self.calls.append(
            OutgoingCall("answer_callback_query", callback_query_id=callback_query_id, text=text)
        )
        return True

    async def send_chat_action(self, chat_id, action, **kwargs):
        self.calls.append(OutgoingCall("send_chat_action", chat_id=chat_id, text=action))
        return True

    # ---- aiogram method-object path ----
    async def __call__(self, method, request_timeout=None):
        title = type(method).__name__
        if title == "SendMessage":
            return await self.send_message(
                method.chat_id,
                method.text,
                getattr(method, "reply_markup", None),
                getattr(method, "parse_mode", None),
            )
        if title == "AnswerCallbackQuery":
            return await self.answer_callback_query(
                method.callback_query_id, getattr(method, "text", None)
            )
        if title == "SendChatAction":
            return await self.send_chat_action(method.chat_id, method.action)
        self.calls.append(OutgoingCall(title, payload=method))
        return True

    # ---- assertion helpers ----
    def messages(self) -> list[OutgoingCall]:
        return [c for c in self.calls if c.method == "send_message"]

    def texts(self) -> list[str]:
        return [c.text for c in self.messages() if c.text]

    def texts_to(self, chat_id: int) -> list[str]:
        return [c.text for c in self.messages() if c.chat_id == chat_id and c.text]

    def last_message(self) -> OutgoingCall | None:
        messages = self.messages()
        return messages[-1] if messages else None

    def last_reply_markup(self):
        last = self.last_message()
        return last.reply_markup if last is not None else None

    def contains(self, needle: str) -> bool:
        return any(needle in text for text in self.texts())

    def premium_messages(self) -> list[OutgoingCall]:
        """Sent messages that actually carry a premium emoji tag."""
        return [call for call in self.messages() if call.text and TG_EMOJI_RE.search(call.text)]

    def premium_texts(self) -> list[str]:
        return [call.text for call in self.premium_messages()]

    def messages_with_markup(self) -> list[OutgoingCall]:
        return [call for call in self.messages() if call.reply_markup is not None]

    def all_buttons(self) -> list:
        """Every button of every keyboard sent so far."""
        return [button for call in self.messages() for button in iter_buttons(call.reply_markup)]


class ScriptedExtraction:
    """Fake ``ExtractionService`` whose behaviour is scripted per test."""

    def __init__(self, result=None, *, api_key="test-openrouter-key", error=None, results=None):
        self.api_key = api_key
        self.error = error
        if results is not None:
            self._results = list(results)
        else:
            self._results = [] if result is None else [result]
        self.calls: list[str] = []

    async def extract(self, text, session_id=None):
        self.calls.append(text)
        if self.error is not None:
            raise self.error
        if not self._results:
            raise AssertionError("ScriptedExtraction has no scripted result left")
        return self._results.pop(0)

    async def close(self):
        return None


class RecordingSheets:
    """Fake sheets backend: records synced leads, returns a scripted row."""

    def __init__(self, row: int | None = 7):
        self.row = row
        self.synced: list[Lead] = []

    async def sync_lead(self, lead):
        self.synced.append(lead)
        return self.row

    async def close(self):
        return None


async def wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.01) -> bool:
    """Poll ``predicate`` until true. Used for fire-and-forget sync tasks."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return bool(predicate())


class DispatcherHarness:
    """Feeds synthetic updates into the real dispatcher and inspects the result."""

    def __init__(self, container, bot, extraction, sheets):
        self.container = container
        self.bot = bot
        self.extraction = extraction
        self.sheets = sheets
        self._update_id = 0

    # ---------- update plumbing ----------
    def _next_update_id(self) -> int:
        self._update_id += 1
        return self._update_id

    @staticmethod
    def _message_payload(message_id: int, text: str, user_id: int, chat_id: int) -> dict:
        return {
            "message_id": message_id,
            "date": BASE_DATE,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "text": text,
        }

    async def feed(self, payload: dict):
        """Validate a raw payload and push it through the real dispatcher."""
        return await self.container.dp.feed_update(self.bot, Update.model_validate(payload))

    async def send_text(self, text: str, *, user_id: int = OWNER_USER_ID, chat_id: int | None = None):
        chat_id = chat_id if chat_id is not None else user_id
        payload = {
            "update_id": self._next_update_id(),
            "message": self._message_payload(self._update_id, text, user_id, chat_id),
        }
        return await self.feed(payload)

    async def send_command(self, command: str, *, user_id: int = OWNER_USER_ID, chat_id: int | None = None):
        return await self.send_text(command, user_id=user_id, chat_id=chat_id)

    async def tap(self, callback_data: str, *, user_id: int = OWNER_USER_ID, chat_id: int | None = None):
        chat_id = chat_id if chat_id is not None else user_id
        update_id = self._next_update_id()
        payload = {
            "update_id": update_id,
            "callback_query": {
                "id": f"callback-{update_id}",
                "chat_instance": "test-chat-instance",
                "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
                "data": callback_data,
                "message": self._message_payload(update_id, "card", user_id, chat_id),
            },
        }
        return await self.feed(payload)

    # ---------- state inspection ----------
    async def fsm_state(self, *, user_id: int = OWNER_USER_ID, chat_id: int | None = None):
        chat_id = chat_id if chat_id is not None else user_id
        context = self.container.dp.fsm.get_context(
            bot=self.bot, chat_id=chat_id, user_id=user_id
        )
        return await context.get_state()

    async def fsm_data(self, *, user_id: int = OWNER_USER_ID, chat_id: int | None = None) -> dict:
        chat_id = chat_id if chat_id is not None else user_id
        context = self.container.dp.fsm.get_context(
            bot=self.bot, chat_id=chat_id, user_id=user_id
        )
        return await context.get_data()

    # ---------- database inspection ----------
    async def leads(self) -> list[Lead]:
        async with self.container.session_factory() as session:
            result = await session.execute(select(Lead).order_by(Lead.id))
            return list(result.scalars().all())

    async def lead_count(self) -> int:
        return len(await self.leads())

    async def sessions(self) -> list[LeadSession]:
        async with self.container.session_factory() as session:
            result = await session.execute(select(LeadSession).order_by(LeadSession.id))
            return list(result.scalars().all())


def _fresh_dispatcher_module():
    """Give each test its own Router.

    ``app.bot.handlers.router`` is a module-level singleton and aiogram forbids
    attaching one Router to two Dispatchers ("Router is already attached to ...").
    Reloading the bot modules re-executes the decorators on a brand-new Router,
    so every test gets clean, isolated dispatcher wiring.
    """
    import app.bot.handlers as handlers_module

    importlib.reload(handlers_module)
    return importlib.reload(dispatcher_module)


@pytest_asyncio.fixture
async def harness(tmp_path, monkeypatch):
    """Real container on a tmp SQLite DB (Alembic schema) + fake I/O boundaries."""
    db_path = tmp_path / "integration.db"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"

    monkeypatch.setenv("BOT_TOKEN", TEST_BOT_TOKEN)
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("ALLOWED_USER_IDS", str(OWNER_USER_ID))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.setenv("GOOGLE_SHEET_ID", "")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    monkeypatch.setenv("GOOGLE_SHEETS_WEBHOOK_URL", "")
    monkeypatch.setenv("GOOGLE_SHEETS_WEBHOOK_TOKEN", "")
    monkeypatch.setenv("COLLECT_TIMEOUT_SECONDS", "60")
    get_settings.cache_clear()

    # Real Alembic migrations, applied to the throwaway database file.
    await run_migrations_async()

    settings = get_settings()
    assert settings.DATABASE_URL == url, "integration test must not touch the real database"

    container = Container(settings)
    dispatcher = _fresh_dispatcher_module()
    dispatcher.setup_dispatcher(container)

    # Swap the network-capable collaborators for recording fakes.
    real_bot, real_extraction, real_sheets = container.bot, container.extraction, container.sheets
    bot = RecordingBot()
    extraction = ScriptedExtraction()
    sheets = RecordingSheets()
    container.bot, container.extraction, container.sheets = bot, extraction, sheets

    harness = DispatcherHarness(container, bot, extraction, sheets)
    try:
        yield harness
    finally:
        await container.session_buffer.shutdown()
        # Fire-and-forget tasks (flow uses ``asyncio.create_task(sync_and_notify)``)
        # hold aiosqlite sessions; destroying them mid-await leaks work into the
        # next test (symptoms: "coroutine ... was never awaited", "Event loop is
        # closed" raised inside an aiosqlite worker thread). Drain them first.
        current = asyncio.current_task()
        pending = [task for task in asyncio.all_tasks() if task is not current]
        if pending:
            await asyncio.wait(pending, timeout=5)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        with suppress(Exception):
            await real_extraction.close()
        with suppress(Exception):
            await real_sheets.close()
        with suppress(Exception):
            await real_bot.session.close()
        await container.engine.dispose()
        get_settings.cache_clear()
