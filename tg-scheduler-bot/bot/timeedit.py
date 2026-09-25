"""Разбор нового времени для уже записанного события.

Человек присылает одно из:

    1500              — новое начало, длительность сохраняется
    1500-1600         — начало и конец явно
    завтра            — другой день, время прежнее
    26.09 1500        — другой день и новое начало
    завтра 1500-1600  — другой день, начало и конец

Разбирается здесь, без обращения к модели: правило жёсткое и проверяемое,
а ошибка в нём двигает реальное событие в календаре.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

# Время в тех же написаниях, что и в списках дел: 9 | 9:30 | 930 | 1800 | 15.55
_TIME = r"(\d{1,2})(?:[:.\s]?(\d{2}))?"
_DASH = r"\s*[-–—]\s*"
TIME_PART = re.compile(rf"^{_TIME}(?:{_DASH}{_TIME})?$")

# Дата: 26.09, 26.9, 26.09.2026, 26/09
DATE_PART = re.compile(r"^(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?$")

# Относительные дни считаются от сегодняшнего, а не от дня события:
# «завтра» человек говорит про завтра, где бы событие ни лежало.
RELATIVE_DAYS = {
    "вчера": -1,
    "сегодня": 0,
    "завтра": 1,
    "послезавтра": 2,
}

HINT = (
    "Не понял. Пришли «1500» — сдвину начало, «1500-1600» — поставлю начало "
    "и конец, «завтра» или «26.09» — перенесу на другой день. Можно вместе: "
    "«завтра 1500»"
)


class TimeEditError(ValueError):
    """Текст не похож на время или дату. Сообщение уходит пользователю как есть."""


def _moment(day: datetime, hours: str, minutes: str | None) -> datetime:
    hour = int(hours)
    minute = int(minutes) if minutes else 0
    if hour > 23 or minute > 59:
        raise TimeEditError(f"{hour:02d}:{minute:02d} — такого времени не бывает")
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _parse_date(word: str, today: date) -> date | None:
    """Дата из слова. None — если это не дата."""
    shift = RELATIVE_DAYS.get(word.lower())
    if shift is not None:
        return today + timedelta(days=shift)

    match = DATE_PART.match(word)
    if match is None:
        return None

    day, month, year = match.groups()
    if year is None:
        # Года нет — берём ближайший подходящий: для планировщика «26.12»
        # в январе почти наверняка про декабрь наступившего года.
        candidate = _make_date(int(day), int(month), today.year)
        if candidate < today:
            candidate = _make_date(int(day), int(month), today.year + 1)
        return candidate

    number = int(year)
    return _make_date(int(day), int(month), number + 2000 if number < 100 else number)


def _make_date(day: int, month: int, year: int) -> date:
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise TimeEditError(f"{day:02d}.{month:02d}.{year} — такой даты нет") from exc


def parse_new_time(
    text: str, start: datetime, end: datetime, today: date
) -> tuple[datetime, datetime]:
    """Новое время события по присланному тексту.

    Одно время — это новое начало, длительность при этом сохраняется: так
    «перенести на 15:00» не превращает часовое дело в минутное. Дата без
    времени переносит событие на другой день, не трогая часы.
    """
    words = (text or "").split()
    if not words:
        raise TimeEditError(HINT)

    day = _parse_date(words[0], today)
    rest = words[1:] if day is not None else words
    # «1500 - 1600» человек пишет и с пробелами вокруг тире.
    time_text = "".join(rest)

    if not time_text:
        if day is None:
            raise TimeEditError(HINT)
        # Только дата: часы остаются прежними.
        return _on_date(start, day), _on_date(end, day + (end.date() - start.date()))

    match = TIME_PART.match(time_text)
    if match is None:
        raise TimeEditError(HINT)

    anchor = _on_date(start, day) if day is not None else start
    start_hours, start_minutes, end_hours, end_minutes = match.groups()
    new_start = _moment(anchor, start_hours, start_minutes)

    if end_hours is None:
        return new_start, new_start + (end - start)

    new_end = _moment(anchor, end_hours, end_minutes)
    if new_end <= new_start:
        # Через полночь события бот не пишет, так что это опечатка.
        raise TimeEditError("Конец раньше начала — проверь порядок")
    return new_start, new_end


def _on_date(moment: datetime, day: date) -> datetime:
    """То же время суток, но в другой день."""
    return moment.replace(year=day.year, month=day.month, day=day.day)


def replace_line(text: str, name: str, value: str) -> str:
    """Заменяет одну строку VEVENT, оставляя остальное нетронутым.

    Строки с параметрами (``DESCRIPTION;ALTREP=...``) тоже наша цель:
    иначе рядом с новым значением осталось бы старое.
    """
    seen = False
    lines = []
    for line in text.splitlines():
        if line.split(";")[0].split(":")[0].upper() == name:
            if not seen:
                lines.append(f"{name}:{value}")
                seen = True
        else:
            lines.append(line)

    if not seen:
        # Поля не было вовсе — дописываем перед концом события.
        try:
            where = lines.index("END:VEVENT")
        except ValueError as exc:
            raise TimeEditError("в событии нет END:VEVENT") from exc
        lines.insert(where, f"{name}:{value}")
    return "\r\n".join(lines) + "\r\n"


def retime_ics(text: str, start: datetime, end: datetime) -> str:
    """Переписывает DTSTART и DTEND в готовом VEVENT, не трогая остальное.

    UID сохраняется: у события остаётся та же личность, по которой его
    находит пара в Google. Время пишется в UTC, как и при создании.
    """
    replacements = {
        "DTSTART": start.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "DTEND": end.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
    }

    seen = set()
    lines = []
    for line in text.splitlines():
        name = line.split(";")[0].split(":")[0].upper()
        if name in replacements:
            lines.append(f"{name}:{replacements[name]}")
            seen.add(name)
        else:
            lines.append(line)

    missing = sorted(set(replacements) - seen)
    if missing:
        raise TimeEditError(f"в событии нет полей {', '.join(missing)}")
    return "\r\n".join(lines) + "\r\n"
