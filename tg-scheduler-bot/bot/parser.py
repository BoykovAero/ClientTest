"""Разбор свободного текста в список событий через GPT.

Модель получает текущую дату, день недели и тайм-зону пользователя — без этого
«завтра в 15:00» разбирается мимо (контракт A.6). Ответ запрашивается строго
в JSON и валидируется здесь же: всё, что не прошло проверку, отбрасывается,
а не уезжает в календарь.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI, OpenAIError

from bot.calendars.base import Event
from bot.llm import describe

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
- Если событие на весь день, all_day = true, а start и end — даты YYYY-MM-DD \
(end равен start, если событие однодневное).
- Если время окончания не названо, end не указывай — длительность подставится сама.
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
    for index, item in enumerate(raw_events):
        if not isinstance(item, dict):
            logger.warning("Разбор: элемент %d не объект, пропускаю", index)
            continue

        title = str(item.get("title") or "").strip()
        if not title:
            logger.warning("Разбор: элемент %d без заголовка, пропускаю", index)
            continue

        all_day = bool(item.get("all_day"))
        try:
            start = _parse_moment(str(item.get("start") or ""), tz, all_day)
        except (ValueError, TypeError):
            logger.warning(
                "Разбор: у %r неразборчивое начало %r, пропускаю",
                title,
                item.get("start"),
            )
            continue

        end = None
        raw_end = item.get("end")
        if raw_end:
            try:
                end = _parse_moment(str(raw_end), tz, all_day)
            except (ValueError, TypeError):
                logger.warning("Разбор: у %r неразборчивый конец %r", title, raw_end)

        if end is None or end < start or (not all_day and end == start):
            end = start if all_day else start + timedelta(minutes=default_minutes)

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
        self, text: str, now: datetime | None = None, instruction: str = ""
    ) -> list[Event]:
        """Текст -> список событий. Бросает ParseError, если модель не ответила."""
        moment = now or datetime.now(self._tz)
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
