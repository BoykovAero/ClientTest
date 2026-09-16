"""Чтение и валидация конфигурации из .env.

Единственное место, где читается окружение. Всё остальное приложение получает
готовый объект Config и на os.environ не смотрит.
"""

from __future__ import annotations

import base64
import binascii
import json
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
    google_service_account_info: dict | None
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


def _load_service_account(problems: list[str]) -> dict | None:
    """Ключ сервисного аккаунта из переменной окружения или из файла.

    На облачных хостингах диска для файлов обычно нет, поэтому JSON кладут
    целиком в GOOGLE_SERVICE_ACCOUNT_JSON. На своём сервере удобнее файл.
    """
    raw_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    raw_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()

    if raw_json:
        source = "GOOGLE_SERVICE_ACCOUNT_JSON"
        # Ключ принимается и как обычный JSON, и как base64 от него. Второе —
        # страховка от панелей хостингов, которые калечат многострочные
        # значения: внутри private_key есть переводы строк.
        if not raw_json.lstrip().startswith("{"):
            try:
                raw_json = base64.b64decode(raw_json, validate=True).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError, ValueError):
                problems.append(
                    f"{source}: значение не похоже ни на JSON (должно начинаться "
                    "с фигурной скобки), ни на base64 от него"
                )
                return None
        try:
            info = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            problems.append(f"{source}: не разбирается как JSON ({exc})")
            return None
    elif raw_path:
        path = BASE_DIR / raw_path
        source = str(path)
        if not path.exists():
            problems.append(
                f"Не найден {path}. Это JSON-ключ сервисного аккаунта из "
                "Google Cloud Console (DEPLOY.md, шаг 4)."
            )
            return None
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            problems.append(f"{source}: не читается как JSON ({exc})")
            return None
    else:
        return None

    if not isinstance(info, dict):
        problems.append(f"{source}: ожидается JSON-объект")
        return None

    # Без этих полей google-auth упадёт уже в рантайме, на первом событии.
    absent = [key for key in ("client_email", "private_key") if not info.get(key)]
    if absent:
        problems.append(
            f"{source}: в ключе нет полей {', '.join(absent)} — "
            "скачай JSON-ключ заново в Google Cloud Console"
        )
        return None

    return info


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
    google_calendar_id = os.environ.get("GOOGLE_CALENDAR_ID", "primary").strip() or "primary"

    # В Google можно ходить двумя способами. Сервисный аккаунт не требует
    # браузера и не протухает — он предпочтителен для сервера; OAuth-токен
    # остаётся для тех, у кого уже есть готовый token.json.
    #
    # Ключ сервисного аккаунта берётся либо прямо из переменной окружения
    # (так его задают на облачных хостингах, где нет диска для файлов), либо
    # из файла. Переменная имеет приоритет.
    google_service_account_info = _load_service_account(problems)

    if google_service_account_info is not None:
        if google_calendar_id == "primary":
            # У сервисного аккаунта свой собственный пустой primary-календарь,
            # и события ушли бы в него, а не к пользователю.
            problems.append(
                "GOOGLE_CALENDAR_ID=primary несовместим с сервисным аккаунтом: "
                "укажи адрес своего календаря (обычно это твой gmail) и открой "
                "ему доступ на изменение событий (DEPLOY.md, шаг 4)."
            )
    elif not google_token_file.exists():
        problems.append(
            "Нет доступа к Google: не задан ни GOOGLE_SERVICE_ACCOUNT_JSON, ни "
            f"GOOGLE_SERVICE_ACCOUNT_FILE, и не найден {google_token_file.name}. "
            "Нужен либо ключ сервисного аккаунта, либо token.json от "
            "python3 google_auth_setup.py (DEPLOY.md, шаг 4)."
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
        google_service_account_info=google_service_account_info,
        google_calendar_id=google_calendar_id,
        timezone=timezone,
        daily_prompt_time=daily_prompt_time,
        default_event_minutes=default_event_minutes,
        log_level=os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO",
    )
