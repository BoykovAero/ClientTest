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


# Насколько большой разрыв ещё считается продолжением предыдущего дела.
# Голое время — это конец: «1430-1700 мат», «1830 рус» значит, что русский
# идёт от 17:00 до 18:30. Но если от предыдущего конца прошло полдня, дело
# явно не длилось всё это время — тогда берём обычную длительность.
MAX_CHAIN_GAP = timedelta(hours=2)


def chain_start(end: datetime, previous_end: datetime | None, default_minutes: int) -> datetime:
    """Начало дела, у которого назван только конец."""
    if (
        previous_end is not None
        and previous_end < end
        and end - previous_end < MAX_CHAIN_GAP
    ):
        return previous_end
    return end - timedelta(minutes=default_minutes)


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
    # Учёба, Работа, Развитие или Личное. Пусто — не опознано; такое событие
    # идёт в основной календарь без цвета.
    category: str = ""

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
        """Описание события — только то, что написал человек.

        Метку происхождения сюда не кладём: она попадалась на глаза в
        календаре без всякой пользы. Свои события бот узнаёт по id в Google
        и по UID в iCloud — их не видно.
        """
        return self.notes.strip()

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


@dataclass(frozen=True)
class CalendarEntry:
    """Запись календаря, показанная пользователю для выбора.

    ``uid`` заполнен только у событий, созданных ботом: по нему находится
    их пара в iCloud. У чужих событий его нет, и удалить получится лишь
    ту копию, которую видно.

    ``start`` и ``end`` нужны, чтобы событие можно было подвинуть: новое
    время считается от старого. У события на весь день их нет — такое не
    двигается по часам.
    """

    event_id: str
    when: str
    title: str
    uid: str = ""
    start: datetime | None = None
    end: datetime | None = None
    notes: str = ""
    # Из какого календаря запись: правки адресуются туда же.
    calendar_id: str = ""
    category: str = ""

    @property
    def movable(self) -> bool:
        """Можно ли назначить событию новое время."""
        return self.start is not None and self.end is not None

    def as_line(self) -> str:
        return f"{self.when}  {self.title}" if self.when else self.title
