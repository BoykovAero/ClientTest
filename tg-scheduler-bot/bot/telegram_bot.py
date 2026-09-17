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

from bot.calendars.base import CalendarEntry, CalendarError, Event, SaveResult
from bot.documents import DocumentError, extract_text, is_large, kind_of
from bot.llm import WORKS, WORKS_WITH_VISION, available_models, probe_all
from bot.parser import ParseError
from bot.sheets import SheetsError, find_link, strip_link
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
    "Ещё можно прислать файл с расписанием — Word, Excel, таблицу или "
    "скриншот — либо ссылку на Google Таблицу. Я разберу и покажу список, "
    "прежде чем записывать.\n\n"
    "Команды:\n"
    "/plan — спросить прямо сейчас\n"
    "/today — что записано на сегодня, с удалением\n"
    "/models — какие модели доступны твоему ключу"
)

# Ключ в user_data, под которым ждут подтверждения разобранные из файла события.
PENDING_KEY = "pending_events"
# Сколько событий показать кнопками: у Telegram предел на размер клавиатуры.
DELETE_LIMIT = 20
# Сколько событий показать в списке: у Telegram предел на длину сообщения.
PREVIEW_LIMIT = 30


class SchedulerBot:
    def __init__(
        self, config, parser, transcriber, image_reader, openai_client, google, icloud,
        sheets=None,
    ) -> None:
        self._config = config
        self._sheets = sheets
        self._openai_client = openai_client
        self._parser = parser
        self._transcriber = transcriber
        self._image_reader = image_reader
        self._google = google
        self._icloud = icloud
        # Разобранные из файлов события ждут здесь подтверждения. Память
        # процесса: после перезапуска список просто считается устаревшим.
        self._pending: dict[str, list[Event]] = {}
        # Показанные списки событий: по номеру из них выбирают, что удалить.
        self._listings: dict[str, list[CalendarEntry]] = {}

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

        token = uuid.uuid4().hex[:8]
        self._listings[token] = items[:DELETE_LIMIT]

        lines = [f"Сегодня, {now:%d.%m}:", ""]
        lines += [f"{index + 1}. {entry.as_line()}" for index, entry in enumerate(items)]
        if len(items) > DELETE_LIMIT:
            lines.append("")
            lines.append(f"Удалить можно первые {DELETE_LIMIT}.")
        lines.append("")
        lines.append("Чтобы удалить — нажми на номер.")

        await update.message.reply_text(
            "\n".join(lines), reply_markup=self._number_keyboard(token, len(self._listings[token]))
        )

    @staticmethod
    def _number_keyboard(token: str, count: int) -> InlineKeyboardMarkup:
        """Ряды кнопок с номерами событий, по пять в ряд."""
        buttons = [
            InlineKeyboardButton(str(number), callback_data=f"pick:{token}:{number}")
            for number in range(1, count + 1)
        ]
        rows = [buttons[i : i + 5] for i in range(0, len(buttons), 5)]
        return InlineKeyboardMarkup(rows)

    async def on_pick(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Выбрано событие из списка — спрашиваем подтверждение."""
        if not self._authorized(update):
            return

        query = update.callback_query
        await query.answer()
        _, _, rest = (query.data or "").partition(":")
        token, _, number = rest.partition(":")
        entry = self._entry(token, number)

        if entry is None:
            await query.edit_message_text(
                f"{query.message.text}\n\n(список устарел — открой /today заново)"
            )
            return

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Удалить", callback_data=f"kill:{token}:{number}"),
                    InlineKeyboardButton("Отмена", callback_data=f"keep:{token}:{number}"),
                ]
            ]
        )
        # Правим то же сообщение, а не шлём новое: иначе чат зарастает
        # списками и подтверждениями.
        await query.edit_message_text(
            f"Удалить «{entry.title}» ({entry.when})?\n\n"
            "Событие пропадёт из обоих календарей, вернуть его я не смогу.",
            reply_markup=keyboard,
        )

    async def on_delete(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Подтверждение или отказ от удаления."""
        if not self._authorized(update):
            return

        query = update.callback_query
        action, _, rest = (query.data or "").partition(":")
        token, _, number = rest.partition(":")
        entry = self._entry(token, number)

        if entry is None:
            await query.answer()
            await query.edit_message_text("Список устарел — открой /today заново.")
            return

        if action == "keep":
            await query.answer("Оставил")
            await _drop(query.message)
            return

        await query.answer()
        await query.edit_message_text(f"Удаляю «{entry.title}»…")
        results = await self._remove(entry)

        if all("✗" not in line for line in results):
            # Всё получилось — сообщение убираем, итог показываем подсказкой.
            await query.answer(f"Удалено: {entry.title}", show_alert=False)
            await _drop(query.message)
            return

        # Что-то не вышло — такое сообщение должно остаться на виду.
        await query.edit_message_text(
            f"«{entry.title}» ({entry.when})\n" + " · ".join(results)
        )

    def _entry(self, token: str, number: str) -> CalendarEntry | None:
        """Событие по номеру из списка. None — если список устарел."""
        entries = self._listings.get(token)
        if not entries or not number.isdigit():
            return None
        index = int(number) - 1
        return entries[index] if 0 <= index < len(entries) else None

    async def _remove(self, entry: CalendarEntry) -> list[str]:
        """Удаляет событие из обоих календарей, по строке отчёта на каждый."""
        results = []
        try:
            await asyncio.to_thread(self._google.delete, entry.event_id)
            results.append("Google ✓")
        except CalendarError as exc:
            logger.warning("Не удалось удалить в Google: %s", exc)
            results.append(f"Google ✗ ({exc})")

        if not entry.uid:
            # Чужое событие: пары в iCloud у него нет.
            results.append("iCloud — пары нет")
            return results

        try:
            removed = await asyncio.to_thread(self._icloud.delete_by_uid, entry.uid)
            results.append("iCloud ✓" if removed else "iCloud — уже не было")
        except CalendarError as exc:
            logger.warning("Не удалось удалить в iCloud: %s", exc)
            results.append(f"iCloud ✗ ({exc})")
        return results

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

        link = find_link(text)
        if link is not None:
            await self._from_sheet(update, *link, instruction=strip_link(text))
            return

        await self._process(update, text)

    async def _from_sheet(
        self, update: Update, sheet_id: str, gid: str, instruction: str
    ) -> None:
        """Ссылка на Google Таблицу: читаем её и предлагаем найденное."""
        await self._typing(update)
        notice = await update.message.reply_text("Открываю таблицу…")

        try:
            text = await asyncio.to_thread(self._sheets.read, sheet_id, gid)
        except SheetsError as exc:
            logger.warning("Таблица не прочитана: %s", exc)
            await _drop(notice)
            await update.message.reply_text(f"Не смог прочитать таблицу: {exc}")
            return

        logger.info("Таблица прочитана, %d символов", len(text))
        await _drop(notice)
        await self._propose(update, text, instruction)

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
        # Подпись к файлу — это просьба: «добавь расписание 11е инж».
        await self._propose(update, text, (message.caption or "").strip())

    async def _propose(self, update: Update, text: str, instruction: str = "") -> None:
        """Разбирает текст и показывает список, не записывая ничего сразу.

        Извлечение из файла ошибается чаще, чем разбор короткого сообщения,
        поэтому здесь всегда спрашиваем подтверждение.
        """
        await self._typing(update)
        notice = None
        if is_large(text):
            # Большой файл разбирается частями и это заметно по времени.
            notice = await update.message.reply_text(
                "Расписание большое, разбираю по частям — это займёт до минуты…"
            )

        try:
            events = await self._parser.parse(text, instruction=instruction)
        except ParseError as exc:
            logger.warning("Разбор файла не удался: %s", exc)
            await _drop(notice)
            await update.message.reply_text(f"Не смог разобрать расписание: {exc}")
            return

        if not events:
            hint = (
                " Уточни подписью, что именно взять — например «расписание 11Е на понедельник»."
                if not instruction
                else ""
            )
            await _drop(notice)
            await update.message.reply_text(
                f"В файле не нашлось подходящих дел с датой или временем.{hint}"
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
        await _drop(notice)
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
            await _drop(query.message)
            return

        await query.edit_message_text(f"{query.message.text}\n\nЗаписываю…")
        reports = []
        for event in events:
            results = await self._save(event)
            reports.append(self._format_event(event, results))

        header = f"{_plural(len(events))}:"
        await _drop(query.message)
        await query.message.chat.send_message(header + "\n\n" + "\n\n".join(reports))

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


async def _drop(message) -> None:
    """Убирает временное уведомление. Его пропажа — мелочь, падать не из-за чего."""
    if message is None:
        return
    try:
        await message.delete()
    except TelegramError:
        pass


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
