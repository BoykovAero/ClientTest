"""Распознавание голосовых сообщений через Whisper.

Telegram отдаёт голосовые в OGG/Opus — Whisper API принимает такой файл
напрямую, перекодировать через ffmpeg не нужно (контракт A.5).
"""

from __future__ import annotations

import logging

from openai import AsyncOpenAI, OpenAIError

logger = logging.getLogger(__name__)

# Ограничение Whisper API. Голосовое в Telegram ограничено длительностью
# и до этого предела не дотягивает, но проверка дешёвая.
MAX_AUDIO_BYTES = 25 * 1024 * 1024


class TranscriptionError(RuntimeError):
    """Не удалось распознать речь."""


class Transcriber:
    def __init__(self, client: AsyncOpenAI, model: str) -> None:
        self._client = client
        self._model = model

    async def transcribe(self, audio: bytes, filename: str = "voice.ogg") -> str:
        """Байты голосового -> текст. Бросает TranscriptionError."""
        if not audio:
            raise TranscriptionError("пустой аудиофайл")
        if len(audio) > MAX_AUDIO_BYTES:
            raise TranscriptionError(
                f"файл больше {MAX_AUDIO_BYTES // (1024 * 1024)} МБ"
            )

        try:
            result = await self._client.audio.transcriptions.create(
                model=self._model,
                file=(filename, audio),
                # Бот русскоязычный: явный язык заметно повышает точность
                # на коротких записях и убирает случайные переключения.
                language="ru",
            )
        except OpenAIError as exc:
            raise TranscriptionError(_describe(exc)) from exc

        text = (getattr(result, "text", "") or "").strip()
        if not text:
            raise TranscriptionError("в записи не распознано ни слова")

        logger.info("Whisper: распознано %d символов", len(text))
        return text


def _describe(exc: OpenAIError) -> str:
    status = getattr(exc, "status_code", None)
    known = {
        401: "ключ OpenAI неверный или отозван",
        413: "запись слишком большая",
        429: "лимит или нулевой баланс OpenAI",
    }
    if status in known:
        return f"{status}: {known[status]}"
    return str(exc).split("\n", 1)[0][:200] or "ошибка Whisper"
