"""Общее для обращений к языковым моделям: расшифровка ошибок провайдера.

Идентификаторы моделей у провайдеров меняются — снятую с обслуживания модель
сервис отдаёт как 404 model_not_found. Такое сообщение само по себе тупик,
поэтому здесь к нему подставляется список моделей, доступных этому ключу.
"""

from __future__ import annotations

import logging

from openai import AsyncOpenAI, OpenAIError

logger = logging.getLogger(__name__)

# Сколько имён моделей показать пользователю: полный список у провайдеров
# бывает в сотню строк, а в сообщение Telegram это не влезет.
MODELS_SHOWN = 12

KNOWN_STATUSES = {
    401: "ключ неверный или отозван",
    403: "ключу закрыт доступ",
    413: "запрос слишком большой",
    429: "исчерпан лимит или квота",
    500: "сбой на стороне провайдера",
    503: "провайдер перегружен",
}


def _status(exc: OpenAIError) -> int | None:
    return getattr(exc, "status_code", None)


# Оба статуса провайдеры отдают и когда модели нет, и когда она есть, но
# закрыта тарифом. Отличить одно от другого по тексту ненадёжно, поэтому
# ниже мы просто спрашиваем список доступных моделей и смотрим фактам в лицо.
MODEL_STATUSES = (403, 404)


async def available_models(client: AsyncOpenAI) -> list[str]:
    """Имена моделей, доступных ключу. Пустой список, если спросить не вышло."""
    try:
        response = await client.models.list()
    except OpenAIError:
        logger.warning("Не удалось получить список моделей", exc_info=True)
        return []
    return sorted(model.id for model in response.data)


# Обрывать список ради одного-двух имён бессмысленно: «и ещё 1» занимает
# столько же места, сколько само имя, а пользы не несёт.
TAIL_TOLERANCE = 3


def _format_models(names: list[str]) -> tuple[str, str]:
    """Имена для показа и хвост вида « и ещё N»."""
    if len(names) <= MODELS_SHOWN + TAIL_TOLERANCE:
        return ", ".join(names), ""
    return ", ".join(names[:MODELS_SHOWN]), f" и ещё {len(names) - MODELS_SHOWN}"


async def describe(exc: OpenAIError, client: AsyncOpenAI, model: str) -> str:
    """Короткое объяснение ошибки — оно уходит пользователю в Telegram."""
    status = _status(exc)

    if status in MODEL_STATUSES:
        names = await available_models(client)
        if names:
            # В обоих случаях человеку нужно одно и то же: чем заменить.
            others = [name for name in names if name != model]
            shown, more = _format_models(others)
            reason = (
                f"модель {model!r} числится доступной, но провайдер отказал "
                f"({status}) — похоже, она закрыта твоим тарифом"
                if model in names
                else f"модель {model!r} недоступна"
            )
            hint = f" Попробуй другую: {shown}{more}." if others else ""
            return f"{reason}.{hint} Имя модели задаётся переменной окружения"
        if not names:
            # Список не получить, но статус всё равно указывает на модель —
            # лучше сказать это, чем вывалить сырой текст провайдера.
            return (
                f"модель {model!r} недоступна ({status}), "
                "а список моделей у провайдера получить не удалось"
            )

    if status in KNOWN_STATUSES:
        return f"{status}: {KNOWN_STATUSES[status]}"
    return str(exc).split("\n", 1)[0][:200] or "ошибка провайдера"
