"""Запись событий в iCloud-календарь по CalDAV.

Календарь iCloud синхронизируется с приложением «Календарь» на Mac, поэтому
событие, созданное здесь, появляется на Mac само.

iCalendar-объект собирается вручную, а не через save_event(**kwargs): так мы
управляем UID и делаем запись идемпотентной — CalDAV-сервер хранит событие
по UID, и второй раз то же событие не создаётся.

Библиотека caldav блокирующая: публичные методы вызываются из потока
(asyncio.to_thread) и сериализуются одним замком.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

import caldav
from caldav.lib import error as caldav_error

from bot.calendars.base import MARKER_PREFIX, CalendarError, Event, SaveResult

logger = logging.getLogger(__name__)

TARGET = "iCloud"
PRODID = f"-//{MARKER_PREFIX}//RU"


class ICloudCalendar:
    def __init__(
        self, url: str, apple_id: str, app_password: str, calendar_name: str = ""
    ) -> None:
        self._url = url
        self._apple_id = apple_id
        self._app_password = app_password
        self._calendar_name = calendar_name
        self._lock = threading.Lock()
        self._calendar = None

    # ─── подключение ────────────────────────────────────────────────────────
    def _get_calendar(self):
        if self._calendar is not None:
            return self._calendar

        client = caldav.DAVClient(
            url=self._url, username=self._apple_id, password=self._app_password
        )
        try:
            calendars = client.principal().calendars()
        except caldav_error.AuthorizationError as exc:
            raise CalendarError(
                "401: неверный Apple ID или пароль. Нужен app-specific password "
                "с appleid.apple.com, а не обычный пароль"
            ) from exc
        except Exception as exc:
            raise CalendarError(f"нет связи с iCloud: {exc}") from exc

        if not calendars:
            raise CalendarError("в аккаунте iCloud нет ни одного календаря")

        if self._calendar_name:
            for calendar in calendars:
                if str(calendar.name or "").strip() == self._calendar_name:
                    self._calendar = calendar
                    break
            else:
                available = ", ".join(str(c.name) for c in calendars)
                raise CalendarError(
                    f"календарь {self._calendar_name!r} не найден. Доступны: {available}"
                )
        else:
            self._calendar = calendars[0]
            if len(calendars) > 1:
                # Молча взять первый из нескольких — верный способ потом
                # искать события не в том календаре.
                logger.warning(
                    "iCloud: ICLOUD_CALENDAR_NAME не задан, пишу в %r. "
                    "Доступны: %s",
                    str(self._calendar.name),
                    ", ".join(repr(str(c.name)) for c in calendars),
                )

        logger.info("iCloud: работаю с календарём %r", str(self._calendar.name))
        return self._calendar

    # ─── операции ───────────────────────────────────────────────────────────
    def check(self) -> None:
        """Проверка доступа при старте. Бросает CalendarError."""
        with self._lock:
            self._get_calendar()

    def save(self, event: Event) -> SaveResult:
        """Создаёт событие. Повторный вызов с тем же событием не плодит дубль."""
        with self._lock:
            try:
                calendar = self._get_calendar()
                if self._exists(calendar, event.uid):
                    logger.info("iCloud: событие %r уже существует", event.title)
                    return SaveResult.already_exists(TARGET)

                calendar.save_event(build_ics(event))
                logger.info("iCloud: создано событие %r (uid=%s)", event.title, event.uid)
                return SaveResult.created(TARGET)
            except CalendarError as exc:
                logger.warning("iCloud: не удалось создать %r: %s", event.title, exc)
                return SaveResult.failed(TARGET, str(exc))
            except Exception as exc:
                logger.exception("iCloud: непредвиденная ошибка на %r", event.title)
                return SaveResult.failed(TARGET, str(exc))

    @staticmethod
    def _exists(calendar, uid: str) -> bool:
        try:
            calendar.event_by_uid(uid)
            return True
        except caldav_error.NotFoundError:
            return False
        except Exception:
            # Сервер ответил неожиданно — считаем, что события нет, и пробуем
            # создать: дубль хуже, чем потерянное событие, но UID всё равно
            # защитит от него на стороне сервера.
            logger.debug("iCloud: не смог проверить наличие uid=%s", uid, exc_info=True)
            return False


# ─── сборка iCalendar ───────────────────────────────────────────────────────
def _escape(text: str) -> str:
    """Экранирование по RFC 5545: обратный слэш, точка с запятой, запятая, перевод строки."""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def _fold(line: str) -> str:
    """Складывает длинную строку по 75 октетов, не разрывая UTF-8 символы."""
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    limit = 75
    for char in line:
        char_len = len(char.encode("utf-8"))
        if current_len + char_len > limit:
            chunks.append("".join(current))
            current = [char]
            current_len = char_len
            limit = 74  # продолжения начинаются с пробела
        else:
            current.append(char)
            current_len += char_len
    chunks.append("".join(current))
    return "\r\n ".join(chunks)


def build_ics(event: Event) -> str:
    """Собирает VCALENDAR с одним VEVENT.

    Время пишется в UTC (суффикс Z): так не нужен компонент VTIMEZONE,
    а клиент покажет событие в своей локальной зоне.
    """
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    if event.all_day:
        dtstart = f"DTSTART;VALUE=DATE:{event.start_date():%Y%m%d}"
        dtend = f"DTEND;VALUE=DATE:{event.end_date_exclusive():%Y%m%d}"
    else:
        start = event.start.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        end = event.end.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dtstart = f"DTSTART:{start}"
        dtend = f"DTEND:{end}"

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:{event.uid}",
        f"DTSTAMP:{now}",
        dtstart,
        dtend,
        _fold(f"SUMMARY:{_escape(event.title)}"),
        _fold(f"DESCRIPTION:{_escape(event.description())}"),
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"
