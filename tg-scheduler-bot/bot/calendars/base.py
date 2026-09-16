"""Общая модель события и детерминированный идентификатор.

Идентификатор считается из содержимого события, поэтому повторная обработка
того же сообщения не плодит дубли: и Google, и CalDAV отказываются создавать
второе событие с тем же id/UID (контракт A.2.7).
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta

# Префикс в описании события — по нему видно происхождение записи глазами,
# в отличие от id, который в интерфейсе календаря не показывается.
MARKER_PREFIX = "tg-scheduler-bot"

# Перевод из стандартного алфавита base32 в base32hex. Готовая
# base64.b32hexencode появилась только в Python 3.10, а macOS до сих пор
# приносит с собой 3.9 — держим совместимость своими силами.
_B32_TO_B32HEX = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567", "0123456789ABCDEFGHIJKLMNOPQRSTUV"
)


class CalendarError(RuntimeError):
    """Календарь недоступен или отказал. Ловится на уровне хендлера."""


@dataclass(frozen=True)
class Event:
    """Событие, готовое к записи.

    Для события на весь день start и end — даты; end хранится включительно
    (последний день события), приведение к эксклюзивной границе делает каждый
    бэкенд сам, потому что форматы у них разные.
    """

    title: str
    start: datetime
    end: datetime
    all_day: bool = False
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("У события пустой заголовок")
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("Время события должно быть с тайм-зоной")
        if self.end < self.start:
            raise ValueError("Конец события раньше начала")

    @property
    def uid(self) -> str:
        """Детерминированный ключ события.

        base32hex в нижнем регистре: алфавит [0-9a-v] — ровно то, что Google
        Calendar принимает в качестве заданного клиентом id события.
        """
        key = "|".join(
            [
                self.title.strip().casefold(),
                self.start.isoformat(),
                self.end.isoformat(),
                "all-day" if self.all_day else "timed",
            ]
        )
        digest = hashlib.sha1(key.encode("utf-8")).digest()
        encoded = base64.b32encode(digest).decode("ascii").translate(_B32_TO_B32HEX)
        return encoded.lower().rstrip("=")

    @property
    def marker(self) -> str:
        """Строка-метка для описания события."""
        return f"[{MARKER_PREFIX}:{self.uid}]"

    def description(self) -> str:
        """Описание события: заметки пользователя плюс метка происхождения."""
        return f"{self.notes.strip()}\n\n{self.marker}".strip()

    def start_date(self) -> date:
        return self.start.date()

    def end_date_exclusive(self) -> date:
        """Следующий день после последнего дня события.

        И Google, и iCalendar задают конец события на весь день эксклюзивно.
        """
        return self.end.date() + timedelta(days=1)

    def human_range(self, tz) -> str:
        """Человекочитаемый интервал для ответа в Telegram."""
        start = self.start.astimezone(tz)
        end = self.end.astimezone(tz)
        if self.all_day:
            if start.date() == end.date():
                return start.strftime("%d.%m") + ", весь день"
            return f"{start.strftime('%d.%m')}–{end.strftime('%d.%m')}, весь день"
        if start.date() == end.date():
            return f"{start.strftime('%d.%m %H:%M')}–{end.strftime('%H:%M')}"
        return f"{start.strftime('%d.%m %H:%M')} – {end.strftime('%d.%m %H:%M')}"


@dataclass(frozen=True)
class SaveResult:
    """Результат записи одного события в один календарь."""

    target: str
    ok: bool
    duplicate: bool = False
    error: str = ""

    @classmethod
    def created(cls, target: str) -> "SaveResult":
        return cls(target=target, ok=True)

    @classmethod
    def already_exists(cls, target: str) -> "SaveResult":
        return cls(target=target, ok=True, duplicate=True)

    @classmethod
    def failed(cls, target: str, error: str) -> "SaveResult":
        return cls(target=target, ok=False, error=error)

    def as_line(self) -> str:
        if self.duplicate:
            return f"{self.target} ✓ (уже было)"
        if self.ok:
            return f"{self.target} ✓"
        return f"{self.target} ✗ ({self.error})"
