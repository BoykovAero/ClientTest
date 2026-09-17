"""Хендлеры Telegram: команды, текст, голос, ежедневный вопрос.

Два сквозных правила (контракт A.2):
  - первая строка каждого хендлера — проверка авторизации;
  - ни одна ошибка внешнего сервиса не роняет процесс: пользователь получает
    внятный ответ, подробности уходят в лог.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from bot.calendars.base import CalendarError, Event, SaveResult
from bot.documents import DocumentError, extract_text, kind_of
from bot.llm import WORKS, WORKS_WITH_VISION, available_models, probe_all
from bot.parser import ParseError
from bot.transcribe import TranscriptionError
from bot.vision import VisionError

logger = logging.getLogger(__name__)

DAILY_QUESTION = (
    "Доброе утро. Как пройдёт день?\n\n"
    "Ответь текстом или голосовым — разложу по календарям."
)

START_TEXT = (
    "Я планировщик дня.\n\n"
    "Каждый день в {time} спрошу, как пройдёт день. Ответишь текстом или "
    "голосовым — разберу и запишу события в Google Calendar и в iCloud.\n\n"
    "Ещё можно прислать файл с расписанием: Word, Excel, таблицу или "
    "скриншот. Я разберу его и покажу список, прежде чем записывать.\n\n"
    "Команды:\n"
    "/plan — спросить прямо сейчас\n"
    "/today — что уже записано на сегодня\n"
    "/models — какие модели доступны твоему ключу"
)

# Ключ в user_data, под которым ждут подтверждения разобранные из файла события.
PENDING_KEY = "pending_events"
# Сколько событий показать в списке: у Telegram предел на длину сообщения.
PREVIEW_LIMIT = 30


class SchedulerBot:
    def __init__(
        self, config, parser, transcriber, image_reader, openai_client, google, icloud
    ) -> None:
        self._config = config
        self._openai_client = openai_client
        self._parser = parser
        self._transcriber = transcriber
        self._image_reader = image_reader
        self._google = google
        self._icloud = icloud
        # Разобранные из файлов события ждут здесь подтверждения. Память
        # процесса: после перезапуска список просто считается устаревшим.
        self._pending: dict[str, list[Event]] = {}

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

    async def models(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Проверяет каждую модель ключа настоящим запросом.

        Список от провайдера включает и закрытое тарифом, и то, что вообще не
        для переписки. Угадывать по именам — долго и неточно, поэтому просто
        пробуем.
        """
        if not self._authorized(update):
            return

        await self._typing(update)
        names = await available_models(self._openai_client)
        if not names:
            await update.message.reply_text(
                "Не удалось получить список моделей — проверь ключ и OPENAI_BASE_URL."
            )
            return

        notice = await update.message.reply_text(
            f"Проверяю {len(names)} моделей, это займёт несколько секунд…"
        )
        results = await probe_all(self._openai_client, names)

        ok_verdicts = (WORKS, WORKS_WITH_VISION)
        working = [(name, verdict) for name, verdict in results if verdict in ok_verdicts]
        refused = [(name, verdict) for name, verdict in results if verdict not in ok_verdicts]
        seeing = [name for name, verdict in working if verdict == WORKS_WITH_VISION]

        lines = []
        if working:
            lines.append("Работают:")
            for name, verdict in working:
                mark = " (читает картинки)" if verdict == WORKS_WITH_VISION else ""
                lines.append(f"  ✓ {name}{mark}")
            lines.append("")
            lines.append(f"Для текста стоит: {self._config.openai_model}")
            lines.append(f"Для картинок стоит: {self._config.openai_vision_model}")
            if not seeing:
                lines.append("")
                lines.append(
                    "Ни одна доступная модель не читает картинки. "
                    "Присылай расписания файлами .xlsx или .docx — их я разбираю сам."
                )
        else:
            lines.append("Ни одна модель не ответила — похоже, дело в ключе или тарифе.")
        if refused:
            lines.append("")
            lines.append("Недоступны:")
            lines += [f"  ✗ {name} — {verdict}" for name, verdict in refused]

        await notice.edit_text("\n".join(lines))

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

    async def on_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Файл с расписанием: Word, Excel, таблица, текст или картинка."""
        if not self._authorized(update):
            return

        message = update.message
        document = message.document
        photo = message.photo[-1] if message.photo else None
        if document is None and photo is None:
            return

        filename = (document.file_name if document else "") or "снимок.jpg"
        mime_type = (document.mime_type if document else "") or "image/jpeg"
        kind = "image" if photo is not None else kind_of(filename, mime_type)

        if kind == "unsupported":
            await message.reply_text(
                f"Не знаю, что делать с файлом {filename!r}.\n\n"
                "Пришли .docx, .xlsx, .csv, .txt или картинку со скриншотом расписания."
            )
            return

        await self._typing(update)
        try:
            source = await context.bot.get_file(
                (document or photo).file_id
            )
            data = bytes(await source.download_as_bytearray())
        except TelegramError as exc:
            logger.warning("Не удалось скачать файл %r: %s", filename, exc)
            await message.reply_text("Не смог скачать файл, попробуй ещё раз.")
            return

        try:
            if kind == "image":
                text = await self._image_reader.read(data, mime_type)
            else:
                text = await asyncio.to_thread(extract_text, filename, data)
        except (DocumentError, VisionError) as exc:
            logger.warning("Файл %r не прочитан: %s", filename, exc)
            await message.reply_text(f"Не смог прочитать файл: {exc}")
            return

        logger.info("Файл %r прочитан, %d символов", filename, len(text))
        await self._propose(update, text)

    async def _propose(self, update: Update, text: str) -> None:
        """Разбирает текст и показывает список, не записывая ничего сразу.

        Извлечение из файла ошибается чаще, чем разбор короткого сообщения,
        поэтому здесь всегда спрашиваем подтверждение.
        """
        await self._typing(update)
        try:
            events = await self._parser.parse(text)
        except ParseError as exc:
            logger.warning("Разбор файла не удался: %s", exc)
            await update.message.reply_text(f"Не смог разобрать расписание: {exc}")
            return

        if not events:
            await update.message.reply_text(
                "В файле не нашлось ни одного дела с датой или временем."
            )
            return

        token = uuid.uuid4().hex[:12]
        self._pending[token] = events

        lines = [f"Нашёл {_plural_found(len(events))}:", ""]
        for event in events[:PREVIEW_LIMIT]:
            lines.append(f"• {event.title} — {event.human_range(self._config.timezone)}")
        if len(events) > PREVIEW_LIMIT:
            lines.append(f"…и ещё {len(events) - PREVIEW_LIMIT}")
        lines.append("")
        lines.append("Записывать в календари?")

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Записать", callback_data=f"save:{token}"),
                    InlineKeyboardButton("Отмена", callback_data=f"drop:{token}"),
                ]
            ]
        )
        await update.message.reply_text("\n".join(lines), reply_markup=keyboard)

    async def on_decision(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Нажатие «Записать» или «Отмена» под разобранным файлом."""
        if not self._authorized(update):
            return

        query = update.callback_query
        await query.answer()
        action, _, token = (query.data or "").partition(":")
        events = self._pending.pop(token, None)

        if events is None:
            # Бот перезапускался, либо кнопку нажали второй раз.
            await query.edit_message_text(
                f"{query.message.text}\n\n(список устарел — пришли файл заново)"
            )
            return

        if action == "drop":
            await query.edit_message_text(f"{query.message.text}\n\nОтменено.")
            return

        await query.edit_message_text(f"{query.message.text}\n\nЗаписываю…")
        reports = []
        for event in events:
            results = await self._save(event)
            reports.append(self._format_event(event, results))

        header = f"{_plural(len(events))}:"
        await query.message.reply_text(header + "\n\n" + "\n\n".join(reports))

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


def _plural_found(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return f"{count} событие"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return f"{count} события"
    return f"{count} событий"


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
