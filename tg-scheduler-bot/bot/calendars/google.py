"""Запись событий в Google Calendar.

Клиент google-api-python-client блокирующий, поэтому все публичные методы
вызываются из потока (asyncio.to_thread) и сериализуются одним замком.

OAuth здесь не проходится: бот только читает готовый token.json и обновляет
его по refresh-токену (контракт A.4). Сам token.json получают один раз на
машине пользователя через google_auth_setup.py.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from bot.calendars.base import CalendarError, Event, SaveResult

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]
TARGET = "Google"


class GoogleCalendar:
    def __init__(self, token_file: Path, calendar_id: str, timezone_name: str) -> None:
        self._token_file = token_file
        self._calendar_id = calendar_id
        self._timezone_name = timezone_name
        self._lock = threading.Lock()
        self._credentials_cache: Credentials | None = None
        self._service = None

    # ─── авторизация ────────────────────────────────────────────────────────
    def _credentials(self) -> Credentials:
        """Действительные учётные данные.

        Объект Credentials создаётся один раз и дальше обновляется на месте.
        Это важно: собранный сервис держит ссылку именно на него, поэтому
        перечитывание файла заново давало бы сервису устаревший объект,
        а запись обновлённого токена на диск теряла бы смысл.
        """
        if self._credentials_cache is None:
            if not self._token_file.exists():
                raise CalendarError(
                    f"нет {self._token_file.name}, запусти google_auth_setup.py"
                )
            self._credentials_cache = Credentials.from_authorized_user_file(
                str(self._token_file), SCOPES
            )

        creds = self._credentials_cache
        if creds.valid:
            return creds
        if not (creds.expired and creds.refresh_token):
            raise CalendarError(
                "токен недействителен и не может быть обновлён — "
                "перезапусти google_auth_setup.py"
            )

        logger.info("Google: токен истёк, обновляю по refresh_token")
        creds.refresh(Request())
        # Обновлённый токен обязательно сохранить: иначе после перезапуска
        # сервиса рефреш начнётся с нуля, а при отзыве старого всё встанет.
        self._token_file.write_text(creds.to_json(), encoding="utf-8")
        self._token_file.chmod(0o600)
        logger.info("Google: токен обновлён и сохранён")
        return creds

    def _get_service(self):
        creds = self._credentials()
        if self._service is None:
            self._service = build(
                "calendar", "v3", credentials=creds, cache_discovery=False
            )
        return self._service

    # ─── операции ───────────────────────────────────────────────────────────
    def check(self) -> None:
        """Проверка доступа при старте. Бросает CalendarError."""
        with self._lock:
            try:
                self._get_service().calendars().get(
                    calendarId=self._calendar_id
                ).execute()
            except CalendarError:
                raise
            except HttpError as exc:
                raise CalendarError(_describe(exc)) from exc
            except Exception as exc:  # сеть, TLS, что угодно
                raise CalendarError(str(exc)) from exc

    def save(self, event: Event) -> SaveResult:
        """Создаёт событие. Повторный вызов с тем же событием не плодит дубль."""
        with self._lock:
            try:
                service = self._get_service()
                service.events().insert(
                    calendarId=self._calendar_id, body=self._to_body(event)
                ).execute()
                logger.info("Google: создано событие %r (id=%s)", event.title, event.uid)
                return SaveResult.created(TARGET)
            except HttpError as exc:
                if _status(exc) == 409:
                    # id занят: событие уже создавалось. Google держит id
                    # занятым и после удаления события — это тоже 409.
                    logger.info("Google: событие %r уже существует", event.title)
                    return SaveResult.already_exists(TARGET)
                logger.warning("Google: не удалось создать %r: %s", event.title, exc)
                return SaveResult.failed(TARGET, _describe(exc))
            except CalendarError as exc:
                return SaveResult.failed(TARGET, str(exc))
            except Exception as exc:
                logger.exception("Google: непредвиденная ошибка на %r", event.title)
                return SaveResult.failed(TARGET, str(exc))

    def list_day(self, day_start: datetime) -> list[tuple[str, str]]:
        """События за сутки от day_start. Возвращает пары (время, заголовок)."""
        with self._lock:
            try:
                response = (
                    self._get_service()
                    .events()
                    .list(
                        calendarId=self._calendar_id,
                        timeMin=day_start.isoformat(),
                        timeMax=(day_start + timedelta(days=1)).isoformat(),
                        singleEvents=True,
                        orderBy="startTime",
                    )
                    .execute()
                )
            except HttpError as exc:
                raise CalendarError(_describe(exc)) from exc
            except CalendarError:
                raise
            except Exception as exc:
                raise CalendarError(str(exc)) from exc

        items = []
        for item in response.get("items", []):
            start = item.get("start", {})
            when = "весь день" if "date" in start else start.get("dateTime", "")[11:16]
            items.append((when, item.get("summary", "(без названия)")))
        return items

    def _to_body(self, event: Event) -> dict:
        body: dict = {
            "id": event.uid,
            "summary": event.title,
            "description": event.description(),
            # Дублируем метку в приватные свойства: по ним событие можно найти
            # запросом, не разбирая текст описания.
            "extendedProperties": {"private": {"tg_scheduler_uid": event.uid}},
        }
        if event.all_day:
            body["start"] = {"date": event.start_date().isoformat()}
            body["end"] = {"date": event.end_date_exclusive().isoformat()}
        else:
            body["start"] = {
                "dateTime": event.start.isoformat(),
                "timeZone": self._timezone_name,
            }
            body["end"] = {
                "dateTime": event.end.isoformat(),
                "timeZone": self._timezone_name,
            }
        return body


def _status(exc: HttpError) -> int:
    return getattr(getattr(exc, "resp", None), "status", 0) or 0


def _describe(exc: HttpError) -> str:
    """Короткое объяснение ошибки Google — оно уходит пользователю в Telegram."""
    status = _status(exc)
    known = {
        401: "доступ отозван, нужен новый token.json",
        403: "Calendar API не включён или превышена квота",
        404: "календарь не найден, проверь GOOGLE_CALENDAR_ID",
    }
    if status in known:
        return f"{status}: {known[status]}"
    return f"{status}: {getattr(exc, 'reason', None) or 'ошибка Google Calendar'}"
