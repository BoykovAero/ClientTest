"""Чтение и валидация конфигурации из .env.

Единственное место, где читается окружение. Всё остальное приложение получает
готовый объект Config и на os.environ не смотрит.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    """Конфигурация непригодна. Текст пойдёт в лог и в stderr при старте."""


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    allowed_user_id: int

    openai_api_key: str
    openai_model: str
    openai_transcribe_model: str

    icloud_apple_id: str
    icloud_app_password: str
    icloud_caldav_url: str
    icloud_calendar_name: str

    google_credentials_file: Path
    google_token_file: Path
    google_calendar_id: str

    timezone: ZoneInfo
    daily_prompt_time: time
    default_event_minutes: int

    log_level: str

    @property
    def timezone_name(self) -> str:
        return str(self.timezone)


def _required(name: str, missing: list[str]) -> str:
    """Обязательная переменная. Пустую считаем незаполненной."""
    value = os.environ.get(name, "").strip()
    if not value:
        missing.append(name)
    return value


def _parse_time(raw: str, problems: list[str]) -> time:
    """'08:45' -> time(8, 45)."""
    try:
        hours, minutes = raw.split(":")
        return time(hour=int(hours), minute=int(minutes))
    except (ValueError, AttributeError):
        problems.append(f"DAILY_PROMPT_TIME: ожидается ЧЧ:ММ, получено {raw!r}")
        return time(8, 45)


def _parse_positive_int(name: str, raw: str, default: int, problems: list[str]) -> int:
    try:
        value = int(raw)
    except ValueError:
        problems.append(f"{name}: ожидается целое число, получено {raw!r}")
        return default
    if value <= 0:
        problems.append(f"{name}: ожидается положительное число, получено {value}")
        return default
    return value


def load_config(env_file: Path | None = None) -> Config:
    """Читает .env из рабочего каталога и собирает Config.

    Бросает ConfigError со списком всех проблем сразу — чтобы не выяснять их
    по одной, перезапуская сервис.
    """
    load_dotenv(env_file or BASE_DIR / ".env")

    missing: list[str] = []
    problems: list[str] = []

    telegram_bot_token = _required("TELEGRAM_BOT_TOKEN", missing)
    raw_user_id = _required("ALLOWED_USER_ID", missing)
    allowed_user_id = 0
    if raw_user_id:
        allowed_user_id = _parse_positive_int("ALLOWED_USER_ID", raw_user_id, 0, problems)

    openai_api_key = _required("OPENAI_API_KEY", missing)
    icloud_apple_id = _required("ICLOUD_APPLE_ID", missing)
    icloud_app_password = _required("ICLOUD_APP_PASSWORD", missing)

    timezone_name = os.environ.get("TIMEZONE", "Europe/Moscow").strip() or "Europe/Moscow"
    try:
        timezone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        problems.append(
            f"TIMEZONE: неизвестная зона {timezone_name!r}. "
            "На минимальном образе Linux поставь пакет tzdata."
        )
        timezone = ZoneInfo("UTC")

    daily_prompt_time = _parse_time(
        os.environ.get("DAILY_PROMPT_TIME", "08:45").strip(), problems
    )
    default_event_minutes = _parse_positive_int(
        "DEFAULT_EVENT_MINUTES",
        os.environ.get("DEFAULT_EVENT_MINUTES", "60").strip(),
        60,
        problems,
    )

    google_credentials_file = BASE_DIR / os.environ.get(
        "GOOGLE_CREDENTIALS_FILE", "credentials.json"
    )
    google_token_file = BASE_DIR / os.environ.get("GOOGLE_TOKEN_FILE", "token.json")
    if not google_token_file.exists():
        problems.append(
            f"Не найден {google_token_file}. Получи его на своей машине: "
            "python3 google_auth_setup.py (DEPLOY.md, шаг 4.5)."
        )

    if missing:
        problems.insert(0, "Не заданы обязательные переменные: " + ", ".join(missing))
    if problems:
        raise ConfigError(
            "Конфигурация в .env непригодна:\n  - " + "\n  - ".join(problems)
        )

    return Config(
        telegram_bot_token=telegram_bot_token,
        allowed_user_id=allowed_user_id,
        openai_api_key=openai_api_key,
        openai_model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip(),
        openai_transcribe_model=os.environ.get("OPENAI_TRANSCRIBE_MODEL", "whisper-1").strip(),
        icloud_apple_id=icloud_apple_id,
        icloud_app_password=icloud_app_password,
        icloud_caldav_url=os.environ.get("ICLOUD_CALDAV_URL", "https://caldav.icloud.com").strip(),
        icloud_calendar_name=os.environ.get("ICLOUD_CALENDAR_NAME", "").strip(),
        google_credentials_file=google_credentials_file,
        google_token_file=google_token_file,
        google_calendar_id=os.environ.get("GOOGLE_CALENDAR_ID", "primary").strip() or "primary",
        timezone=timezone,
        daily_prompt_time=daily_prompt_time,
        default_event_minutes=default_event_minutes,
        log_level=os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO",
    )
