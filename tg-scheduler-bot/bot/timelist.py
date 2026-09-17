"""Разбор списка дел, записанного временем в начале строки.

    1555-1620 установить клод
    1800 сколково
    22 физ шк

Такой список разбирается здесь, без обращения к модели: правило жёсткое,
и доверять его исполнение модели незачем — она подставляет начало там, где
названо только окончание, и цепочка ломается.

Если хотя бы одна строка не подходит под формат, разбор возвращает None и
работает обычный путь через модель.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from bot.calendars.base import Event

logger = logging.getLogger(__name__)

# «1555-1620 установить клод» или «1800 сколково».
# Время: 22 | 9:30 | 930 | 1800 | 15.55
_TIME = r"(\d{1,2})(?:[:.\s]?(\d{2}))?"
_DASH = r"\s*[-–—]\s*"
LINE = re.compile(rf"^{_TIME}(?:{_DASH}{_TIME})?\s+(\S.*)$")

# Чтобы не принять за расписание случайный текст, требуем несколько строк.
MIN_LINES = 2


def _moment(day: datetime, hours: str, minutes: str | None) -> datetime | None:
    hour = int(hours)
    minute = int(minutes) if minutes else 0
    if hour > 23 or minute > 59:
        return None
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def parse_time_list(
    text: str, now: datetime, tz: ZoneInfo, default_minutes: int
) -> list[Event] | None:
    """Список дел -> события. None, если текст не в этом формате.

    Голое время — это окончание дела: начало берётся от конца предыдущего.
    Диапазон задаёт и начало, и конец.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < MIN_LINES:
        return None

    day = now.astimezone(tz)
    parsed: list[tuple[datetime | None, datetime, str]] = []

    for line in lines:
        match = LINE.match(line)
        if not match:
            return None

        first_h, first_m, second_h, second_m, title = match.groups()
        first = _moment(day, first_h, first_m)
        if first is None:
            return None

        if second_h is not None:
            second = _moment(day, second_h, second_m)
            if second is None or second <= first:
                return None
            parsed.append((first, second, title.strip()))
        else:
            # Одно время — это окончание, начало подставится ниже.
            parsed.append((None, first, title.strip()))

    events: list[Event] = []
    previous_end: datetime | None = None
    for start, end, title in parsed:
        if start is None:
            start = (
                previous_end
                if previous_end is not None and previous_end < end
                else end - timedelta(minutes=default_minutes)
            )
        try:
            events.append(Event(title=_titled(title), start=start, end=end))
        except ValueError as exc:
            logger.warning("Список дел: строка %r отбракована: %s", title, exc)
            continue
        previous_end = end

    if not events:
        return None

    logger.info("Список дел разобран без модели: %d событий", len(events))
    return events


def _titled(title: str) -> str:
    """Первая буква заглавная, остальное как написано."""
    return title[:1].upper() + title[1:] if title else title
