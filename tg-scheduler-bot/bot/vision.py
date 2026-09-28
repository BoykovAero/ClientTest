"""Чтение расписаний с картинок и скриншотов.

Работа в два шага: сначала модель со зрением переписывает изображение в
простой текст, затем этот текст идёт через тот же разбор, что и обычное
сообщение. Так переиспользуется проверенный разбор в JSON, а модели со
зрением не приходится одновременно и читать картинку, и соблюдать схему.
"""

from __future__ import annotations

import base64
import logging

from openai import AsyncOpenAI, OpenAIError

from bot.llm import describe

logger = logging.getLogger(__name__)

# У провайдеров ограничение на картинку в base64 около 4 МБ, а base64
# раздувает данные на треть — отсюда порог на исходный файл.
MAX_IMAGE_BYTES = 3 * 1024 * 1024

PROMPT = """\
На изображении — расписание, план или список дел. Перепиши его простым \
текстом: по одной строке на событие, сохраняя даты, время и названия \
ровно так, как они написаны.

Правила:
- Ничего не придумывай и не додумывай: переноси только то, что видно.
- Не добавляй заголовков, пояснений и комментариев — только сами строки.
- Если на изображении нет расписания или дел, ответь одним словом: НЕТ
"""

NOTHING_FOUND = "НЕТ"


class VisionError(RuntimeError):
    """Не удалось прочитать изображение."""


class ImageReader:
    def __init__(self, client: AsyncOpenAI, model: str) -> None:
        self._client = client
        self._model = model

    async def read(self, image: bytes, mime_type: str = "image/jpeg") -> str:
        """Байты картинки -> текст расписания. Бросает VisionError."""
        if not image:
            raise VisionError("пустой файл")
        if len(image) > MAX_IMAGE_BYTES:
            raise VisionError(
                f"картинка больше {MAX_IMAGE_BYTES // (1024 * 1024)} МБ — "
                "пришли снимок поменьше или сфотографируй по частям"
            )

        encoded = base64.b64encode(image).decode("ascii")
        data_url = f"data:{mime_type or 'image/jpeg'};base64,{encoded}"

        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                temperature=0,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": PROMPT},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    }
                ],
            )
        except OpenAIError as exc:
            raise VisionError(await describe(exc, self._client, self._model)) from exc

        text = (response.choices[0].message.content or "").strip()
        if not text or text.upper().startswith(NOTHING_FOUND):
            raise VisionError("на картинке не видно расписания или списка дел")

        logger.info("Зрение: с картинки снято %d символов", len(text))
        return text
