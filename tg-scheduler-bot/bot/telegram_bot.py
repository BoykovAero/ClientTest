"""Хендлеры Telegram: команды, текст, голос, ежедневный вопрос.

Два сквозных правила (контракт A.2):
  - первая строка каждого хендлера — проверка авторизации;
  - ни одна ошибка внешнего сервиса не роняет процесс: пользователь получает
    внятный ответ, подробности уходят в лог.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from bot.calendars.base import CalendarError, Event, SaveResult
from bot.parser import ParseError
from bot.transcribe import TranscriptionError

logger = logging.getLogger(__name__)

DAILY_QUESTION = (
    "Доброе утро. Как пройдёт день?\n\n"
    "Ответь текстом или голосовым — разложу по календарям."
)

START_TEXT = (
    "Я планировщик дня.\n\n"
    "Каждый день в {time} спрошу, как пройдёт день. Ответишь текстом или "
    "голосовым — разберу и запишу события в Google Calendar и в iCloud.\n\n"
    "Команды:\n"
    "/plan — спросить прямо сейчас\n"
    "/today — что уже записано на сегодня"
)


class SchedulerBot:
    def __init__(self, config, parser, transcriber, google, icloud) -> None:
        self._config = config
        self._parser = parser
        self._transcriber = transcriber
        self._google = google
        self._icloud = icloud

    # ─── авторизация ────────────────────────────────────────────────────────
    def _authorized(self, update: Update) -> bool:
        user = update.effective_user
        if user is not None and user.id == self._config.allowed_user_id:
            return True
        logger.warning(
            "Отклонён апдейт от постороннего user_id=%s",
            getattr(user, "id", "неизвестен"),
        )
        return False

    # ─── команды ────────────────────────────────────────────────────────────
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await update.message.reply_text(
            START_TEXT.format(time=self._config.daily_prompt_time.strftime("%H:%M"))
        )

    async def plan(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await update.message.reply_text(DAILY_QUESTION)

    async def today(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return

        now = datetime.now(self._config.timezone)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            items = await asyncio.to_thread(self._google.list_day, day_start)
        except CalendarError as exc:
            logger.warning("Не удалось прочитать календарь: %s", exc)
            await update.message.reply_text(f"Не смог прочитать календарь: {exc}")
            return

        if not items:
            await update.message.reply_text("На сегодня ничего не записано.")
            return

        lines = [f"Сегодня, {now:%d.%m}:", ""]
        lines += [f"{when}  {title}" if when else title for when, title in items]
        await update.message.reply_text("\n".join(lines))

    # ─── сообщения ──────────────────────────────────────────────────────────
    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        text = (update.message.text or "").strip()
        if not text:
            return
        await self._process(update, text)

    async def on_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return

        voice = update.message.voice or update.message.audio
        if voice is None:
            return

        await self._typing(update)
        try:
            telegram_file = await context.bot.get_file(voice.file_id)
            audio = bytes(await telegram_file.download_as_bytearray())
        except TelegramError as exc:
            logger.warning("Не удалось скачать голосовое: %s", exc)
            await update.message.reply_text("Не смог скачать голосовое, попробуй ещё раз.")
            return

        try:
            text = await self._transcriber.transcribe(audio)
        except TranscriptionError as exc:
            logger.warning("Распознавание не удалось: %s", exc)
            await update.message.reply_text(f"Не смог распознать запись: {exc}")
            return

        # Показываем распознанное: если Whisper ошибся, это сразу видно.
        await update.message.reply_text(f"Распознал:\n{text}")
        await self._process(update, text)

    # ─── ежедневный вопрос ──────────────────────────────────────────────────
    async def daily_question(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            await context.bot.send_message(
                chat_id=self._config.allowed_user_id, text=DAILY_QUESTION
            )
            logger.info("Отправлен ежедневный вопрос")
        except TelegramError as exc:
            logger.error("Не удалось отправить ежедневный вопрос: %s", exc)

    # ─── общий конвейер ─────────────────────────────────────────────────────
    async def _process(self, update: Update, text: str) -> None:
        """Текст -> события -> оба календаря -> отчёт пользователю."""
        await self._typing(update)

        try:
            events = await self._parser.parse(text)
        except ParseError as exc:
            logger.warning("Разбор не удался: %s", exc)
            await update.message.reply_text(f"Не смог разобрать план: {exc}")
            return

        if not events:
            await update.message.reply_text(
                "Не нашёл здесь ни одного дела. Попробуй назвать время и что именно делаешь."
            )
            return

        reports = []
        for event in events:
            results = await self._save(event)
            reports.append(self._format_event(event, results))

        header = f"{_plural(len(events))}:"
        await update.message.reply_text(header + "\n\n" + "\n\n".join(reports))

    async def _save(self, event: Event) -> list[SaveResult]:
        """Пишет событие в оба календаря параллельно.

        Клиенты обоих календарей блокирующие, поэтому уходят в потоки.
        Отказ одного не отменяет запись в другой — частичный успех виден
        в ответе (контракт A.2.6).
        """
        return list(
            await asyncio.gather(
                asyncio.to_thread(self._google.save, event),
                asyncio.to_thread(self._icloud.save, event),
            )
        )

    def _format_event(self, event: Event, results: list[SaveResult]) -> str:
        when = event.human_range(self._config.timezone)
        status = " · ".join(result.as_line() for result in results)
        return f"• {event.title} — {when}\n  {status}"

    @staticmethod
    async def _typing(update: Update) -> None:
        try:
            await update.message.chat.send_action(ChatAction.TYPING)
        except TelegramError:
            pass  # индикатор набора — мелочь, ради неё ничего не ломаем


def _plural(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return f"Записал {count} событие"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return f"Записал {count} события"
    return f"Записал {count} событий"


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Последний рубеж: логируем всё, что не поймали хендлеры.

    Процесс при этом живёт — Restart=always в systemd остаётся страховкой,
    а не штатным способом пережить ошибку (контракт A.2.5).
    """
    logger.exception("Необработанная ошибка", exc_info=context.error)

    message = getattr(update, "message", None)
    if message is None:
        return
    try:
        await message.reply_text("Что-то пошло не так. Подробности в логах сервера.")
    except TelegramError:
        pass
