"""Shared test fixtures and fakes."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

# Ensure tests never read a real .env token or hit the real database.
os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("ALLOWED_USER_IDS", "")
os.environ.setdefault("OPENROUTER_API_KEY", "")
os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_JSON", "")
os.environ.setdefault("GOOGLE_SHEET_ID", "")

from aiogram.fsm.context import FSMContext  # noqa: E402
from aiogram.fsm.storage.base import StorageKey  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402

from app.database import Base  # noqa: E402


@pytest_asyncio.fixture
async def engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


def make_fsm(storage: MemoryStorage | None = None, user_id: int = 1, chat_id: int = 1):
    storage = storage or MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=chat_id, user_id=user_id)
    return storage, FSMContext(storage=storage, key=key)


class FakeBot:
    def __init__(self):
        self.sent: list[tuple] = []

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self.sent.append((chat_id, text, reply_markup))
        return SimpleNamespace(message_id=len(self.sent))


class FakeSettings:
    DEFAULT_CITY = "Алматы"
    COLLECT_TIMEOUT_SECONDS = 7.0
    allowed_user_ids = {1}
    GOOGLE_SHEET_ID = ""
    WEBHOOK_SECRET = ""
    SEARCH_RESULT_LIMIT = 20
    LAST_LEADS_LIMIT = 5


class FakeLeads:
    def __init__(self):
        self.sessions: dict[int, dict] = {}
        self.leads: dict[int, SimpleNamespace] = {}
        self.raw: list[str] = []
        self._sid = 1
        self._lid = 1
        # Mirror of LeadService (FIX-23): the users table knows nobody by default.
        self.users_table: set[int] = set()

    async def is_allowed_user(self, telegram_user_id):
        return telegram_user_id in self.users_table

    async def create_session(self, uid):
        sid = self._sid
        self._sid += 1
        self.sessions[sid] = {"status": "collecting"}
        return sid

    async def update_session(self, sid, **kw):
        if sid in self.sessions:
            self.sessions[sid].update(kw)

    async def cancel_unfinished_sessions(self, statuses=("collecting", "review", "editing")):
        """Mirror of LeadService: open sessions die with the process that held them."""
        ids = sorted(
            sid for sid, data in self.sessions.items() if data.get("status") in statuses
        )
        for sid in ids:
            self.sessions[sid]["status"] = "cancelled"
        return ids

    async def add_raw_message(self, sid, lid, text):
        self.raw.append(text)

    async def add_lead(self, uid, data, sid=None, merge_target_id=None, prefer_new_contact=False):
        # Mirrors LeadService: a merge returns the *duplicate* row, already marked dead.
        lead = SimpleNamespace(
            id=self._lid,
            duplicate_of_id=merge_target_id,
            deleted_at=datetime.now(timezone.utc) if merge_target_id else None,
            sheet_row=None,
        )
        self._lid += 1
        self.leads[lead.id] = lead
        return lead

    async def get_lead(self, lid):
        return self.leads.get(lid)

    async def update_lead(self, lid, **kw):
        return self.leads.get(lid)


class FakeExtraction:
    def __init__(self, result=None, api_key="test"):
        self.api_key = api_key
        self.result = result
        self.calls: list[str] = []

    async def extract(self, text, sid=None):
        self.calls.append(text)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeDedup:
    def __init__(self, match=None):
        self.match = match

    async def find_duplicate(self, fingerprint, owner_id, exclude_lead_id=None):
        return self.match


class FakeSheets:
    async def sync_lead(self, lead):
        return None


class FakeContainer:
    def __init__(self, *, extraction=None, dedup=None, leads=None, bot=None):
        self.bot = bot or FakeBot()
        self.settings = FakeSettings()
        from app.services.background import BackgroundTasks
        from app.services.session_buffer import SessionBufferService

        self.tasks = BackgroundTasks()
        self.session_buffer = SessionBufferService(timeout_seconds=0.05)
        self.leads = leads or FakeLeads()
        self.extraction = extraction or FakeExtraction()
        self.dedup = dedup or FakeDedup()
        self.sheets = FakeSheets()
