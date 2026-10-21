"""Runtime settings, read from environment / server/.env.

Redmine API keys are NOT settings fields: they are looked up per user from
env vars named REDMINE_KEY_USER_<user_id> and never stored in the database.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

SERVER_DIR = Path(__file__).resolve().parent.parent
load_dotenv(SERVER_DIR / ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    tz: str = "America/New_York"

    work_db_path: Path = SERVER_DIR / "data" / "work.db"
    personal_db_path: Path = SERVER_DIR / "data" / "personal.db"
    backup_dir: Path = SERVER_DIR / "backups"
    backup_keep_days: int = 14

    redmine_url: str = "http://127.0.0.1:3000"
    outbox_dry_run: bool = True
    default_deadline_days: int = 7

    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    mqtt_username: str = "server"
    mqtt_password: str = ""
    mqtt_enabled: bool = True

    # Existing 2040Bluetooth firmware, explicitly bound to one user's database.
    pad_enabled: bool = False
    pad_user_id: int = 1
    pad_db: Literal["work", "personal"] = "personal"

    smtp_host: str = "127.0.0.1"
    smtp_port: int = 1025
    smtp_from: str = "eco@localhost"
    alert_email: str = ""  # push-failure alerts; falls back to each user's email

    scheduler_enabled: bool = True
    job_poll_cron: str = "0 */2 * * *"
    job_reminder_cron: str = "0 17 * * mon-fri"
    job_push_cron: str = "0 19 * * mon-fri"
    job_missed_cron: str = "0 0 * * *"
    job_backup_cron: str = "30 2 * * *"

    session_cookie: str = "eco_session"
    session_days: int = 14


@lru_cache
def get_settings() -> Settings:
    return Settings()


def redmine_key_for(user_id: int) -> str | None:
    """Per-user Redmine API key from env/secrets. None = personal-only user."""
    return os.environ.get(f"REDMINE_KEY_USER_{user_id}") or None
