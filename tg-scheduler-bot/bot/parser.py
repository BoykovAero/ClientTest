"""Разбор свободного текста в список событий через GPT.

Модель получает текущую дату, день недели и тайм-зону пользователя — без этого
«завтра в 15:00» разбирается мимо (контракт A.6). Ответ запрашивается строго
в JSON и валидируется здесь же: всё, что не прошло проверку, отбрасывается,
а не уезжает в календарь.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI, OpenAIError

from bot.calendars.base import Event
from bot.llm import describe
from bot.timelist import parse_time_list

logger = logging.getLogger(__name__)

WEEKDAYS_RU = [
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
]

SYSTEM_PROMPT = """\
Ты разбираешь план дня, надиктованный или написанный человеком по-русски, \
в список событий календаря.

Верни СТРОГО JSON-объект вида:
{"events": [{"title": "...", "start": "...", "end": "...", "all_day": false, "notes": "..."}]}

Правила:
- title — короткое название без времени, с заглавной буквы. Например: "Созвон с командой".
- start и end — местное время в формате YYYY-MM-DDTHH:MM:SS, без указания зоны.
- Время, названное с предлогом («в 18:00», «к шести», «в 6 вечера»), — это НАЧАЛО.
- Голое время в начале строки, без предлога («1800 сколково», «22 физ шк»), — это КОНЕЦ дела. В таком случае start не указывай вообще: начало само подставится от конца предыдущего дела в списке.
- «1555-1620 установить клод» — это диапазон: и начало, и конец.
- Если событие на весь день, all_day = true, а start и end — даты YYYY-MM-DD \
(end равен start, если событие однодневное).
- Если названо только начало и не названа длительность, end не указывай — длительность подставится сама.
- notes — уточнения из исходного текста (место, участники). Если их нет, пустая строка.
- Относительные даты («завтра», «в пятницу», «через час») считай от текущего момента, \
он дан в сообщении пользователя.
- «утром» — 09:00, «днём» — 14:00, «вечером» — 19:00, «ночью» — 22:00, если точнее не сказано.
- Время без уточнения части суток трактуй по здравому смыслу: «в 7» про ужин — это 19:00.
- Если в тексте нет ни одного дела, верни {"events": []}.
- Не придумывай события, которых нет в тексте.
- Если пользователь просит выбрать что-то конкретное, возьми только это, а остальное пропусти. Расписание может быть большим — не переноси его целиком.
"""

# Ответ с событиями не должен обрываться на середине: оборванный JSON
# провайдер отвергает целиком.
MAX_RESPONSE_TOKENS = 4000

# Большой текст разбирается частями: маленькие модели не удерживают
# в формате ответ сразу по целому расписанию.
CHUNK_CHARS = 5000
# Верхний предел на число частей — страховка от обработки гигантского файла.
MAX_CHUNKS = 12
# Сколько первых строк считать шапкой и повторять в каждой части: в таблице
# без неё колонки теряют смысл.
HEADER_LINES = 3
# Сколько частей разбирать одновременно.
CHUNK_CONCURRENCY = 3


class ParseError(RuntimeError):
    """Не удалось получить от модели пригодный разбор."""


def build_user_prompt(
    text: str, now: datetime, timezone_name: str, instruction: str = ""
) -> str:
    """Контекст времени, просьба пользователя и сам текст.

    Просьба идёт до текста и отдельным блоком: к файлу её пишут подписью
    («добавь расписание 11е инж»), и без неё модель пытается перенести всё
    расписание целиком.
    """
    weekday = WEEKDAYS_RU[now.weekday()]
    parts = [
        f"Сейчас: {now:%Y-%m-%d %H:%M} ({weekday}), тайм-зона {timezone_name}.",
        f"Сегодня {now:%Y-%m-%d}, завтра {now.date() + timedelta(days=1)}.",
    ]
    if instruction.strip():
        parts.append("")
        parts.append(f"Просьба пользователя: {instruction.strip()}")
    parts.append("")
    parts.append(f"Текст:\n{text}")
    return "\n".join(parts)


def _parse_moment(raw: str, tz: ZoneInfo, all_day: bool) -> datetime:
    """Строка из ответа модели -> datetime с тайм-зоной."""
    value = raw.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    if all_day and len(value) == 10:
        moment = datetime.combine(date.fromisoformat(value), datetime.min.time())
    else:
        moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=tz)
    return moment.astimezone(tz)


def _maybe_moment(
    raw, tz: ZoneInfo, all_day: bool, title: str, label: str
) -> datetime | None:
    """Разбирает момент, если он назван. Неразборчивый считается неназванным."""
    if not raw:
        return None
    try:
        return _parse_moment(str(raw), tz, all_day)
    except (ValueError, TypeError):
        logger.warning("Разбор: у %r неразборчивый(ое) %s %r", title, label, raw)
        return None


def events_from_payload(
    payload: dict, tz: ZoneInfo, default_minutes: int
) -> list[Event]:
    """Валидирует ответ модели и превращает его в события.

    Некорректные элементы пропускаются с записью в лог: одно кривое событие
    не должно ронять разбор всего сообщения.
    """
    raw_events = payload.get("events")
    if not isinstance(raw_events, list):
        raise ParseError("в ответе модели нет списка events")

    events: list[Event] = []
    # Конец последнего дела с временем: от него отсчитывается начало
    # следующего, если названо только время окончания.
    previous_end: datetime | None = None

    for index, item in enumerate(raw_events):
        if not isinstance(item, dict):
            logger.warning("Разбор: элемент %d не объект, пропускаю", index)
            continue

        title = str(item.get("title") or "").strip()
        if not title:
            logger.warning("Разбор: элемент %d без заголовка, пропускаю", index)
            continue

        all_day = bool(item.get("all_day"))
        start = _maybe_moment(item.get("start"), tz, all_day, title, "начало")
        end = _maybe_moment(item.get("end"), tz, all_day, title, "конец")

        if start is None and end is None:
            logger.warning("Разбор: у %r нет ни начала, ни конца, пропускаю", title)
            continue

        if start is None:
            # Назван только конец: «1800 сколково» — значит дело идёт
            # от конца предыдущего до 18:00.
            if previous_end is not None and previous_end < end:
                start = previous_end
            else:
                start = end - timedelta(minutes=default_minutes)

        if end is None or end < start or (not all_day and end == start):
            end = start if all_day else start + timedelta(minutes=default_minutes)

        if not all_day:
            previous_end = end

        try:
            events.append(
                Event(
                    title=title,
                    start=start,
                    end=end,
                    all_day=all_day,
                    notes=str(item.get("notes") or "").strip(),
                )
            )
        except ValueError as exc:
            logger.warning("Разбор: событие %r отбраковано: %s", title, exc)

    return events


def split_into_chunks(
    text: str, chunk_chars: int = CHUNK_CHARS, header_lines: int = HEADER_LINES
) -> list[str]:
    """Режет текст по строкам на части не длиннее chunk_chars.

    Шапка таблицы повторяется в каждой части: без неё строки вида
    «08:30 | 11а | 11б» теряют привязку к колонкам.
    """
    if len(text) <= chunk_chars:
        return [text]

    lines = text.split("\n")
    # Шапка оправдана только если таблица заметно длиннее её самой.
    header = lines[:header_lines] if len(lines) > header_lines * 3 else []
    header_text = "\n".join(header)
    body = lines[len(header):]

    chunks: list[str] = []
    current: list[str] = []
    current_len = len(header_text)

    for line in body:
        if current and current_len + len(line) + 1 > chunk_chars:
            chunks.append("\n".join(header + current) if header else "\n".join(current))
            current = []
            current_len = len(header_text)
        current.append(line)
        current_len += len(line) + 1

    if current:
        chunks.append("\n".join(header + current) if header else "\n".join(current))
    return chunks[:MAX_CHUNKS]


def merge_events(groups: list[list[Event]]) -> list[Event]:
    """Складывает события из частей, отбрасывая повторы.

    На стыке частей одно событие может попасть в обе; UID считается из
    содержимого, поэтому повтор виден без дополнительных ухищрений.
    """
    seen: set[str] = set()
    merged: list[Event] = []
    for group in groups:
        for event in group:
            if event.uid in seen:
                continue
            seen.add(event.uid)
            merged.append(event)
    return sorted(merged, key=lambda event: event.start)


class PlanParser:
    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        tz: ZoneInfo,
        timezone_name: str,
        default_minutes: int,
    ) -> None:
        self._client = client
        self._model = model
        self._tz = tz
        self._timezone_name = timezone_name
        self._default_minutes = default_minutes

    async def parse(
        self,
        text: str,
        now: datetime | None = None,
        instruction: str = "",
        on_progress=None,
    ) -> list[Event]:
        """Текст -> список событий. Большой текст разбирается частями."""
        moment = now or datetime.now(self._tz)

        # Список дел временем в начале строки разбирается сам: правило жёсткое,
        # и модель на нём только путает начало с окончанием.
        direct = parse_time_list(text, moment, self._tz, self._default_minutes)
        if direct is not None:
            return direct

        chunks = split_into_chunks(text)
        if len(chunks) == 1:
            return await self._parse_chunk(chunks[0], moment, instruction)

        logger.info("Разбор: текст поделён на %d частей", len(chunks))
        limit = asyncio.Semaphore(CHUNK_CONCURRENCY)
        done = 0

        async def one(chunk: str) -> list[Event]:
            nonlocal done
            async with limit:
                try:
                    return await self._parse_chunk(chunk, moment, instruction)
                except ParseError as exc:
                    # Одна неудачная часть не должна отменять остальные.
                    logger.warning("Разбор: часть не разобралась: %s", exc)
                    return []
                finally:
                    done += 1
                    if on_progress is not None:
                        await on_progress(done, len(chunks))

        groups = await asyncio.gather(*(one(chunk) for chunk in chunks))
        events = merge_events(list(groups))
        if not events and all(not group for group in groups):
            raise ParseError(
                "ни одна часть файла не разобралась — попробуй сузить просьбу "
                "подписью или прислать кусок расписания поменьше"
            )
        return events

    async def _parse_chunk(
        self, text: str, moment: datetime, instruction: str
    ) -> list[Event]:
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                temperature=0,
                response_format={"type": "json_object"},
                max_tokens=MAX_RESPONSE_TOKENS,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": build_user_prompt(
                            text, moment, self._timezone_name, instruction
                        ),
                    },
                ],
            )
        except OpenAIError as exc:
            raise ParseError(await describe(exc, self._client, self._model)) from exc

        content = (response.choices[0].message.content or "").strip()
        if not content:
            raise ParseError("модель вернула пустой ответ")

        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            logger.warning("Разбор: ответ модели не JSON: %.200s", content)
            raise ParseError("модель вернула не JSON") from exc

        return events_from_payload(payload, self._tz, self._default_minutes)
