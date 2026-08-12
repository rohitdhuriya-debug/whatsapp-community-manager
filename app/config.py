"""Runtime configuration.

Everything sensitive comes from .env - nothing is hardcoded (HC-8).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# What .env.example ships with. If the real .env still holds this, the key
# was never filled in and we must not pretend the app is ready.
OPENROUTER_PLACEHOLDER = "PASTE_YOUR_OPENROUTER_KEY_HERE"

FREE_SUFFIX = ":free"


@dataclass(frozen=True)
class Config:
    openrouter_api_key: str
    waha_api_key: str
    waha_base_url: str
    waha_session: str
    app_host: str
    app_port: int
    timezone: str
    default_model: str
    bearer_token: str
    public_url: str

    @property
    def tz(self) -> ZoneInfo:
        """All schedules and all displayed times use this (HC-5)."""
        return ZoneInfo(self.timezone)

    @property
    def openrouter_ready(self) -> bool:
        key = self.openrouter_api_key
        return bool(key) and key != OPENROUTER_PLACEHOLDER

    @property
    def waha_ready(self) -> bool:
        return bool(self.waha_api_key) and self.waha_api_key != "change-me-to-a-long-random-string"

    @property
    def base_url(self) -> str:
        """Where this app is reachable from a browser."""
        return self.public_url or f"http://localhost:{self.app_port}"

    @property
    def db_path(self) -> Path:
        return BASE_DIR / "app.db"

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"


@lru_cache(maxsize=1)
def get_config() -> Config:
    return Config(
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
        waha_api_key=os.getenv("WAHA_API_KEY", "").strip(),
        waha_base_url=os.getenv("WAHA_BASE_URL", "http://127.0.0.1:3000").rstrip("/"),
        # WAHA Core free tier: exactly one session, and it must be "default".
        waha_session=os.getenv("WAHA_SESSION", "default").strip() or "default",
        app_host=os.getenv("APP_HOST", "127.0.0.1").strip(),
        app_port=int(os.getenv("APP_PORT", "8080")),
        timezone=os.getenv("TIMEZONE", "Asia/Kolkata").strip(),
        default_model=os.getenv(
            "DEFAULT_MODEL", "google/gemma-4-26b-a4b-it:free"
        ).strip(),
        # Empty = auth disabled, which is the default for this single-user app (HC-6).
        bearer_token=os.getenv("APP_BEARER_TOKEN", "").strip(),
        # Set when reachable through a tunnel. Anything that has to hand out an
        # absolute URL - the Google OAuth redirect above all - must use this,
        # or it points at a localhost that only exists on this Mac.
        public_url=os.getenv("PUBLIC_URL", "").strip().rstrip("/"),
    )


def is_free_model(model_id: str | None) -> bool:
    """HC-1: we only ever call OpenRouter models whose id ends in ':free'."""
    return bool(model_id) and model_id.strip().endswith(FREE_SUFFIX)


config = get_config()
