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

    # LLM (OpenRouter)
    OPENROUTER_API_KEY: str = ""

    # Google Sheets
    GOOGLE_SERVICE_ACCOUNT_JSON: str = ""  # base64-encoded service account key
    GOOGLE_SHEET_ID: str = ""

    # Runtime
    DATABASE_URL: str = f"sqlite+aiosqlite:///{(BASE_DIR / 'leadforge.db').as_posix()}"
    DEV_POLLING: bool = False
    COLLECT_TIMEOUT_SECONDS: float = 7.0
    DEFAULT_CITY: str = "Алматы"

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
        if not self.GOOGLE_SERVICE_ACCOUNT_JSON:
            missing.append("GOOGLE_SERVICE_ACCOUNT_JSON")
        if not self.GOOGLE_SHEET_ID:
            missing.append("GOOGLE_SHEET_ID")
        return missing


@lru_cache
def get_settings() -> Settings:
    return Settings()
