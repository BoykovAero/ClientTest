"""Разбор просьбы скопировать дела с одного дня на другой.

    скопируй математику с понедельника на среду
    повтори физику со вторника на завтра и пятницу
    скопируй весь понедельник на среду

Разбирается здесь, без обращения к модели: день и название — вещи
проверяемые, а ошибка в них создаёт лишние события в настоящем календаре.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

# Глагол ищется в начале просьбы: «повтори» в середине плана — это обычное
# дело («повтори билеты в 19»), а не указание скопировать день.
VERB = re.compile(r"^(скопир|копир|продублир|дублир|повтор)")
VERB_LOOKAHEAD = 3

# Понедельник в любом падеже узнаётся по основе: склонений у дней немного,
# но перечислять их все — это список, который однажды разойдётся с жизнью.
WEEKDAY_STEMS = (
    ("понедельник", 0),
    ("вторник", 1),
    ("сред", 2),
    ("четверг", 3),
    ("пятниц", 4),
    ("суббот", 5),
    ("воскресен", 6),
)

RELATIVE_DAYS = {
    "позавчера": -2,
    "вчера": -1,
    "сегодня": 0,
    "завтра": 1,
    "послезавтра": 2,
}

# «с прошлого вторника», «на следующую среду» — сдвигают выбор недели.
PAST_WORDS = re.compile(r"^(прошл|минувш|позапрошл)")
FUTURE_WORDS = re.compile(r"^(следующ|ближайш|будущ)")

DATE_PART = re.compile(r"^(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?$")

FROM_WORDS = {"с", "со"}
TO_WORDS = {"на"}
JOIN_WORDS = {"и", "на"}
# «скопируй весь понедельник» — название не названо, берём день целиком.
WHOLE_DAY = {"все", "всё", "весь", "всю", "день", "целиком", "дела", "всего"}


class CopyError(ValueError):
    """Просьба похожа на копирование, но разобрать её не вышло."""


@dataclass(frozen=True)
class CopyRequest:
    """Что копировать, откуда и куда. Пустой title — весь день."""

    title: str
    source: date
    targets: tuple[date, ...]


def _weekday(word: str) -> int | None:
    for stem, number in WEEKDAY_STEMS:
        if word.startswith(stem):
            return number
    return None


def _nearest(target: int, today: date, forward: bool) -> date:
    """Ближайший такой день недели — вперёд или назад от сегодня."""
    shift = (target - today.weekday()) % 7
    if forward:
        return today + timedelta(days=shift)
    return today - timedelta(days=(today.weekday() - target) % 7)


def _explicit_date(word: str, today: date) -> date | None:
    match = DATE_PART.match(word)
    if match is None:
        return None
    day, month, year = match.groups()
    if year is not None:
        number = int(year)
        return _make(day, month, number + 2000 if number < 100 else number)
    # Года нет — берём ближайший подходящий, как и в правке времени.
    candidate = _make(day, month, today.year)
    if candidate is None:
        return None
    if (candidate - today).days > 180:
        return _make(day, month, today.year - 1) or candidate
    if (today - candidate).days > 180:
        return _make(day, month, today.year + 1) or candidate
    return candidate


def _make(day: str, month: str, year: int) -> date | None:
    try:
        return date(year, int(month), int(day))
    except ValueError:
        return None


def _read_day(words: list[str], start: int, today: date, forward: bool):
    """День, начиная со слова start. Возвращает (дата, сколько слов съели)."""
    index = start
    weeks = 0
    while index < len(words):
        word = words[index]
        if PAST_WORDS.match(word):
            forward = False
            weeks += 1 if word.startswith("позапрошл") else 0
            index += 1
            continue
        if FUTURE_WORDS.match(word):
            forward = True
            index += 1
            continue
        if word == "этой" or word == "эту" or word == "этот":
            index += 1
            continue
        break

    if index >= len(words):
        return None, 0

    word = words[index]
    shift = RELATIVE_DAYS.get(word)
    if shift is not None:
        return today + timedelta(days=shift), index - start + 1

    explicit = _explicit_date(word, today)
    if explicit is not None:
        return explicit, index - start + 1

    number = _weekday(word)
    if number is not None:
        day = _nearest(number, today, forward)
        return day - timedelta(weeks=weeks), index - start + 1

    return None, 0


def looks_like_copy(text: str) -> bool:
    """Начинается ли просьба с «скопируй» и подобного."""
    words = _words(text)
    return any(VERB.match(word) for word in words[:VERB_LOOKAHEAD])


def _words(text: str) -> list[str]:
    cleaned = (text or "").lower().replace("ё", "е")
    return [word.strip(",;:!?()«»\"'") for word in cleaned.split() if word.strip()]


def parse_copy(text: str, today: date) -> CopyRequest | None:
    """Просьба -> что копировать. None — это не про копирование.

    CopyError — просьба про копирование, но день назван невнятно: лучше
    сказать об этом, чем молча разобрать текст как новый план.
    """
    if not looks_like_copy(text):
        return None

    words = _words(text)
    source: date | None = None
    targets: list[date] = []
    title: list[str] = []

    index = 0
    while index < len(words):
        word = words[index]
        if VERB.match(word) and not title and source is None:
            index += 1
            continue
        if word in FROM_WORDS and source is None:
            day, used = _read_day(words, index + 1, today, forward=False)
            if day is not None:
                source = day
                index += 1 + used
                continue
        if word in TO_WORDS:
            day, used = _read_day(words, index + 1, today, forward=True)
            if day is not None:
                targets.append(day)
                index += 1 + used
                # «на среду и пятницу»: подхватываем перечисление.
                while index + 1 < len(words) and words[index] in JOIN_WORDS:
                    more, used = _read_day(words, index + 1, today, forward=True)
                    if more is None:
                        break
                    targets.append(more)
                    index += 1 + used
                continue
        title.append(word)
        index += 1

    if source is None:
        # «скопируй понедельник на среду»: день назван без предлога и осел
        # в названии. Ищем его там, иначе копировать неоткуда.
        source, title = _day_from_title(title, today)

    if source is None and not targets:
        # Ни одного дня — это не просьба о копировании, а обычный план
        # («повтори билеты в 19»). Разбирать его дальше не наше дело.
        return None
    if source is None:
        raise CopyError(
            "Не понял, с какого дня копировать. Напиши так: "
            "«скопируй математику с понедельника на среду»"
        )
    if not targets:
        raise CopyError(
            "Не понял, на какой день копировать. Напиши так: "
            "«скопируй математику с понедельника на среду»"
        )

    name = " ".join(word for word in title if word not in WHOLE_DAY)
    # Повторы убираем, порядок — по датам: «на среду и пятницу» человек
    # читает как список дней, а не как порядок слов.
    unique = tuple(sorted(dict.fromkeys(targets)))
    return CopyRequest(title=name.strip(), source=source, targets=unique)


def _day_from_title(title: list[str], today: date):
    """Вытаскивает день из оставшихся слов. Возвращает (день, остаток)."""
    for index in range(len(title)):
        day, used = _read_day(title, index, today, forward=False)
        if day is not None:
            return day, title[:index] + title[index + used:]
    return None, title


# Короткие слова запроса — предлоги («по математике», «к егэ»): они есть в
# любом названии и только портят поиск.
MIN_QUERY_WORD = 3


def matches(title: str, query: str) -> bool:
    """Дело подходит под названное человеком. Пустой запрос — подходит всё.

    Сравниваем по основам: человек пишет «математику», а в расписании
    стоит «Математика». Полное совпадение тут не работает никогда.
    """
    wanted = [_stem(word) for word in query.split() if len(_key(word)) >= MIN_QUERY_WORD]
    if not wanted:
        return True
    have = [_key(word) for word in title.split()]
    return all(any(word.startswith(stem) for word in have) for stem in wanted)


def _key(value: str) -> str:
    return "".join(ch for ch in (value or "").lower().replace("ё", "е") if ch.isalnum())


def _stem(word: str) -> str:
    """Основа слова: окончание отбрасываем, падеж тут только мешает."""
    key = _key(word)
    return key[: max(4, len(key) - 3)]
