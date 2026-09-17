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


def _is_model_not_found(exc: OpenAIError) -> bool:
    if _status(exc) != 404:
        return False
    return "model" in str(exc).lower()


async def available_models(client: AsyncOpenAI) -> list[str]:
    """Имена моделей, доступных ключу. Пустой список, если спросить не вышло."""
    try:
        response = await client.models.list()
    except OpenAIError:
        logger.warning("Не удалось получить список моделей", exc_info=True)
        return []
    return sorted(model.id for model in response.data)


async def describe(exc: OpenAIError, client: AsyncOpenAI, model: str) -> str:
    """Короткое объяснение ошибки — оно уходит пользователю в Telegram."""
    status = _status(exc)

    if _is_model_not_found(exc):
        names = await available_models(client)
        if not names:
            return f"модель {model!r} недоступна у провайдера"
        shown = ", ".join(names[:MODELS_SHOWN])
        more = f" и ещё {len(names) - MODELS_SHOWN}" if len(names) > MODELS_SHOWN else ""
        return (
            f"модель {model!r} недоступна. Доступны: {shown}{more}. "
            "Поправь переменную окружения с именем модели"
        )

    if status in KNOWN_STATUSES:
        return f"{status}: {KNOWN_STATUSES[status]}"
    return str(exc).split("\n", 1)[0][:200] or "ошибка провайдера"
