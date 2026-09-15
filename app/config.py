"""Application configuration loaded from environment variables / .env file."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """All runtime configuration. Secrets come only from env / .env (gitignored)."""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Telegram
    BOT_TOKEN: str = ""
    ALLOWED_USER_IDS: str = ""  # comma-separated telegram user ids
    WEBHOOK_URL: str = ""
    WEBHOOK_SECRET: str = ""

    # Railway injects this automatically for services with a public domain.
    # Used to build WEBHOOK_URL when it is left empty in webhook mode.
    RAILWAY_PUBLIC_DOMAIN: str = ""

    # LLM (OpenRouter)
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_MODEL: str = "openrouter/free"
    OPENROUTER_FALLBACK_MODEL: str = "nvidia/nemotron-3-ultra-550b-a55b:free"

    # Google Sheets — service-account path (gspread)
    GOOGLE_SERVICE_ACCOUNT_JSON: str = ""  # base64-encoded service account key
    GOOGLE_SHEET_ID: str = ""

    # Google Sheets — Apps Script webhook path (no GCP/billing card required)
    GOOGLE_SHEETS_WEBHOOK_URL: str = ""
    GOOGLE_SHEETS_WEBHOOK_TOKEN: str = ""

    # Runtime
    DATABASE_URL: str = f"sqlite+aiosqlite:///{(BASE_DIR / 'leadforge.db').as_posix()}"
    DEV_POLLING: bool = False
    COLLECT_TIMEOUT_SECONDS: float = 7.0
    DEFAULT_CITY: str = "Алматы"
    # Automatic resync of leads left without a sheet row (the /resync queue).
    # Seconds between passes; 0 or less switches the worker off.
    AUTO_RESYNC_INTERVAL_SECONDS: float = 300.0

    # Timezone used to *display* timestamps (the sheet's «Дата добавления» /
    # «Дата последнего контакта» columns). The database always keeps UTC; only the
    # human-facing text is shifted, otherwise a UTC+5 owner sees times 5 h in the past.
    DISPLAY_TIMEZONE: str = "Asia/Almaty"

    # Public link to the shared sheet, shown in /settings and after a lead is added.
    # Empty (default) = no link is offered; it is never invented.
    SHEET_PUBLIC_URL: str = ""

    # Duplicate notifications shown by /settings. This is the real switch the bot
    # reports — the text used to claim «включены» unconditionally.
    DUP_NOTIFICATIONS_ENABLED: bool = True

    # How many existing leads one dedup lookup may pull out of SQLite (see
    # DedupService). Bounds the in-memory comparison for a large base.
    DEDUP_CANDIDATE_LIMIT: int = 500

    @property
    def allowed_user_ids(self) -> set[int]:
        """Parsed allowlist as a set of ints."""
        if not self.ALLOWED_USER_IDS.strip():
            return set()
        result: set[int] = set()
        for part in self.ALLOWED_USER_IDS.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                result.add(int(part))
            except ValueError:
                continue
        return result

    def missing_secrets(self) -> list[str]:
        """Names of optional-but-important variables that are not configured."""
        missing: list[str] = []
        if not self.BOT_TOKEN:
            missing.append("BOT_TOKEN")
        if not self.OPENROUTER_API_KEY:
            missing.append("OPENROUTER_API_KEY")
        sheets_configured = (
            bool(self.GOOGLE_SERVICE_ACCOUNT_JSON and self.GOOGLE_SHEET_ID)
            or bool(self.GOOGLE_SHEETS_WEBHOOK_URL)
        )
        if not sheets_configured:
            missing.append("GOOGLE_SHEETS (нет ни сервис-аккаунта, ни webhook-URL)")
        return missing


@lru_cache
def get_settings() -> Settings:
    return Settings()


def sheet_public_url(settings) -> str:
    """Public link to the shared sheet, or "" when there is none to show (FIX-15).

    ``SHEET_PUBLIC_URL`` wins; otherwise the spreadsheet URL is derived from
    ``GOOGLE_SHEET_ID`` (path A). Nothing is invented: with neither set, the bot
    simply says the table is not configured.
    """
    explicit = (getattr(settings, "SHEET_PUBLIC_URL", "") or "").strip()
    if explicit:
        return explicit
    sheet_id = (getattr(settings, "GOOGLE_SHEET_ID", "") or "").strip()
    if sheet_id:
        return f"https://docs.google.com/spreadsheets/d/{sheet_id}"
    return ""
