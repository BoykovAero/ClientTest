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
from datetime import datetime, timedelta
from time import monotonic

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import Conflict, TelegramError
from telegram.ext import ContextTypes

from bot.calendars.base import CalendarEntry, CalendarError, Event, SaveResult
from bot.documents import DocumentError, extract_text, is_large, kind_of
from bot.llm import WORKS, WORKS_WITH_VISION, available_models, probe_all
from bot.parser import ParseError, split_into_chunks
from bot.sheets import SheetsError, find_link, looks_like_sheet, strip_link
from bot.timeedit import TimeEditError, parse_new_time
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
    "/today — список дел по дням: листается стрелками, а по нажатию на "
    "номер событие можно перенести, снабдить заметкой или удалить\n"
    "/models — какие модели доступны твоему ключу"
)

# Как часто пытаться снять внезапно появившийся вебхук, секунд.
CONFLICT_RETRY_SECONDS = 60.0
# Время последней такой попытки: Conflict сыплется в каждом цикле поллинга.
_last_conflict_fix = 0.0

# Ключ в user_data, под которым ждут подтверждения разобранные из файла события.
PENDING_KEY = "pending_events"
# Сколько событий показать кнопками: у Telegram предел на размер клавиатуры.
DELETE_LIMIT = 20
# Сколько показанных списков помнить: по ним работают кнопки под сообщениями.
LISTING_MEMORY = 50
# Дни недели для заголовка списка.
WEEKDAYS = (
    "Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье",
)
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
        # Показанные списки событий: по номеру из них выбирают событие.
        # Вместе со списком помним день, чтобы перерисовать его после правки.
        self._listings: dict[str, tuple[int, list[CalendarEntry]]] = {}
        # У кого что сейчас спрошено: id пользователя -> (что, список, номер,
        # сообщение с карточкой). Ответ придёт обычным сообщением.
        self._awaiting: dict[int, tuple[str, str, str, object]] = {}

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
        """Список дел за день. Дальше по нему можно ходить и править."""
        if not self._authorized(update):
            return

        try:
            text, keyboard = await self._day(0)
        except CalendarError as exc:
            logger.warning("Не удалось прочитать календарь: %s", exc)
            await update.message.reply_text(f"Не смог прочитать календарь: {exc}")
            return

        await update.message.reply_text(text, reply_markup=keyboard)

    async def on_day(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Листание по дням — правим то же сообщение, а не шлём новое."""
        if not self._authorized(update):
            return

        query = update.callback_query
        await query.answer()
        _, _, raw = (query.data or "").partition(":")
        try:
            offset = int(raw)
        except ValueError:
            return
        await self._redraw(query.message, offset)

    def _day_start(self, offset: int) -> datetime:
        now = datetime.now(self._config.timezone)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight + timedelta(days=offset)

    async def _day(self, offset: int) -> tuple[str, InlineKeyboardMarkup]:
        """Текст и кнопки для одного дня. CalendarError пробрасывается."""
        day_start = self._day_start(offset)
        items = await asyncio.to_thread(self._google.list_day, day_start)

        token = self._remember(offset, items[:DELETE_LIMIT])
        header = f"{_day_name(offset, day_start)}, {day_start:%d.%m}"

        if not items:
            return f"{header}\n\nНичего не записано.", self._keyboard(token, offset, 0)

        lines = [header, ""]
        lines += [f"{index + 1}. {entry.as_line()}" for index, entry in enumerate(items)]
        if len(items) > DELETE_LIMIT:
            lines.append("")
            lines.append(f"Открыть можно первые {DELETE_LIMIT}.")
        lines.append("")
        lines.append("Нажми на номер, чтобы перенести или удалить.")
        return "\n".join(lines), self._keyboard(token, offset, len(items[:DELETE_LIMIT]))

    async def _redraw(self, message, offset: int, note: str = "") -> None:
        """Перерисовывает день в уже показанном сообщении."""
        try:
            text, keyboard = await self._day(offset)
        except CalendarError as exc:
            logger.warning("Не удалось прочитать календарь: %s", exc)
            await message.edit_text(f"Не смог прочитать календарь: {exc}")
            return

        if note:
            text = f"{note}\n\n{text}"
        try:
            await message.edit_text(text, reply_markup=keyboard)
        except TelegramError:
            # Тот же текст с теми же кнопками Telegram править отказывается —
            # значит на экране и так нужное.
            pass

    def _remember(self, offset: int, entries: list[CalendarEntry]) -> str:
        """Запоминает показанный список, чтобы кнопки знали, на что нажали."""
        token = uuid.uuid4().hex[:8]
        self._listings[token] = (offset, entries)
        # Списки живут в памяти процесса; старые незачем держать вечно.
        while len(self._listings) > LISTING_MEMORY:
            self._listings.pop(next(iter(self._listings)))
        return token

    @staticmethod
    def _keyboard(token: str, offset: int, count: int) -> InlineKeyboardMarkup:
        """Ряд переходов по дням плюс ряды с номерами событий."""
        rows = [
            [
                InlineKeyboardButton("←", callback_data=f"day:{offset - 1}"),
                InlineKeyboardButton("Сегодня", callback_data="day:0"),
                InlineKeyboardButton("→", callback_data=f"day:{offset + 1}"),
            ]
        ]
        buttons = [
            InlineKeyboardButton(str(number), callback_data=f"pick:{token}:{number}")
            for number in range(1, count + 1)
        ]
        rows += [buttons[i : i + 5] for i in range(0, len(buttons), 5)]
        return InlineKeyboardMarkup(rows)

    async def on_pick(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Выбрано событие — показываем, что с ним можно сделать."""
        if not self._authorized(update):
            return

        query = update.callback_query
        await query.answer()
        _, _, rest = (query.data or "").partition(":")
        token, _, number = rest.partition(":")
        entry = self._entry(token, number)

        if entry is None:
            await self._stale(query)
            return

        # Правим то же сообщение, а не шлём новое: иначе чат зарастает
        # списками и карточками.
        await query.edit_message_text(
            self._card_text(entry), reply_markup=self._card_keyboard(entry, token, number)
        )

    @staticmethod
    def _card_text(entry: CalendarEntry, prompt: str = "") -> str:
        lines = [f"«{entry.title}»", entry.when]
        if entry.notes:
            lines.append("")
            lines.append(entry.notes)
        if not entry.movable:
            lines.append("")
            lines.append("Событие на весь день — по часам его не подвинуть.")
        if prompt:
            lines.append("")
            lines.append(prompt)
        return "\n".join(lines)

    @staticmethod
    def _card_keyboard(
        entry: CalendarEntry, token: str, number: str
    ) -> InlineKeyboardMarkup:
        actions = []
        if entry.movable:
            actions.append(
                InlineKeyboardButton("Перенести", callback_data=f"move:{token}:{number}")
            )
        actions.append(
            InlineKeyboardButton(
                "Заметка" if not entry.notes else "Заметку", callback_data=f"note:{token}:{number}"
            )
        )
        actions.append(InlineKeyboardButton("Удалить", callback_data=f"kill:{token}:{number}"))
        return InlineKeyboardMarkup(
            [actions, [InlineKeyboardButton("← к списку", callback_data=f"back:{token}")]]
        )

    async def on_back(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Возврат из карточки события к списку дня."""
        if not self._authorized(update):
            return

        query = update.callback_query
        await query.answer()
        _, _, token = (query.data or "").partition(":")
        known = self._listings.get(token)
        self._awaiting.pop(query.from_user.id, None)
        await self._redraw(query.message, known[0] if known else 0)

    async def on_move(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Спрашиваем новое время и ждём его следующим сообщением."""
        if not self._authorized(update):
            return

        query = update.callback_query
        await query.answer()
        _, _, rest = (query.data or "").partition(":")
        token, _, number = rest.partition(":")
        entry = self._entry(token, number)

        if entry is None or not entry.movable:
            await self._stale(query)
            return

        self._awaiting[query.from_user.id] = ("time", token, number, query.message)
        await query.edit_message_text(
            self._card_text(
                entry,
                prompt=(
                    "Пришли новое время:\n"
                    "• «1500» — сдвину начало, длительность сохраню\n"
                    "• «1500-1600» — начало и конец\n"
                    "• «завтра» или «26.09» — другой день, часы прежние\n"
                    "• «завтра 1500» — и то и другое"
                ),
            ),
            reply_markup=self._waiting_keyboard(token),
        )

    async def on_note(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Спрашиваем заметку и ждём её следующим сообщением."""
        if not self._authorized(update):
            return

        query = update.callback_query
        await query.answer()
        _, _, rest = (query.data or "").partition(":")
        token, _, number = rest.partition(":")
        entry = self._entry(token, number)

        if entry is None:
            await self._stale(query)
            return

        self._awaiting[query.from_user.id] = ("note", token, number, query.message)
        hint = "Пришли заметку — положу её в описание события."
        if entry.notes:
            hint += "\nНовая заменит нынешнюю, «-» сотрёт её совсем."
        await query.edit_message_text(
            self._card_text(entry, prompt=hint), reply_markup=self._waiting_keyboard(token)
        )

    @staticmethod
    def _waiting_keyboard(token: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("← к списку", callback_data=f"back:{token}")]]
        )

    async def _apply_answer(self, update: Update, text: str) -> bool:
        """Присланный текст как ответ на вопрос о событии.

        False — если ничего не спрашивали и текст надо разбирать как план.
        """
        waiting = self._awaiting.get(update.effective_user.id)
        if waiting is None:
            return False

        kind, token, number, card = waiting
        entry = self._entry(token, number)
        if entry is None:
            self._awaiting.pop(update.effective_user.id, None)
            return False

        if kind == "time":
            return await self._answer_time(update, text, entry, token, number, card)
        return await self._answer_note(update, text, entry, token, card)

    async def _answer_time(
        self, update: Update, text: str, entry: CalendarEntry, token, number, card
    ) -> bool:
        if not entry.movable:
            self._awaiting.pop(update.effective_user.id, None)
            return False

        today = datetime.now(self._config.timezone).date()
        try:
            start, end = parse_new_time(text, entry.start, entry.end, today)
        except TimeEditError as exc:
            # Ожидание оставляем: человек просто ошибся в написании.
            await _drop(update.message)
            await card.edit_text(
                self._card_text(entry, prompt=str(exc)),
                reply_markup=self._waiting_keyboard(token),
            )
            return True

        self._awaiting.pop(update.effective_user.id, None)
        # Сообщение с ответом своё дело сделало — в чате ему не место.
        await _drop(update.message)
        results = await self._move(entry, start, end)
        note = f"«{entry.title}» → {start:%d.%m %H:%M}–{end:%H:%M}\n" + " · ".join(results)
        await self._redraw(card, self._offset_of(start), note=note)
        return True

    async def _answer_note(
        self, update: Update, text: str, entry: CalendarEntry, token, card
    ) -> bool:
        self._awaiting.pop(update.effective_user.id, None)
        await _drop(update.message)

        notes = "" if text.strip() == "-" else text.strip()
        results = await self._set_notes(entry, notes)
        known = self._listings.get(token)
        action = "заметка убрана" if not notes else "заметка записана"
        note = f"«{entry.title}» — {action}\n" + " · ".join(results)
        await self._redraw(card, known[0] if known else 0, note=note)
        return True

    def _offset_of(self, moment: datetime) -> int:
        """Насколько день события отстоит от сегодняшнего."""
        return (moment.date() - datetime.now(self._config.timezone).date()).days

    async def _set_notes(self, entry: CalendarEntry, notes: str) -> list[str]:
        """Пишет заметку в оба календаря, по строке отчёта на каждый."""
        results = []
        try:
            await asyncio.to_thread(self._google.set_notes, entry.event_id, notes)
            results.append("Google ✓")
        except CalendarError as exc:
            logger.warning("Не удалось записать заметку в Google: %s", exc)
            results.append(f"Google ✗ ({exc})")

        if self._icloud is None or not entry.uid:
            return results

        try:
            done = await asyncio.to_thread(self._icloud.set_notes_by_uid, entry.uid, notes)
            results.append("iCloud ✓" if done else "iCloud — пары нет")
        except CalendarError as exc:
            logger.warning("Не удалось записать заметку в iCloud: %s", exc)
            results.append(f"iCloud ✗ ({exc})")
        return results

    async def _move(self, entry: CalendarEntry, start, end) -> list[str]:
        """Переносит событие в обоих календарях, по строке отчёта на каждый."""
        results = []
        try:
            await asyncio.to_thread(self._google.move, entry.event_id, start, end)
            results.append("Google ✓")
        except CalendarError as exc:
            logger.warning("Не удалось перенести в Google: %s", exc)
            results.append(f"Google ✗ ({exc})")

        if self._icloud is None:
            return results
        if not entry.uid:
            results.append("iCloud — пары нет")
            return results

        try:
            moved = await asyncio.to_thread(self._icloud.move_by_uid, entry.uid, start, end)
            results.append("iCloud ✓" if moved else "iCloud — пары нет")
        except CalendarError as exc:
            logger.warning("Не удалось перенести в iCloud: %s", exc)
            results.append(f"iCloud ✗ ({exc})")
        return results

    async def _stale(self, query) -> None:
        """Список из памяти уже вытеснен — показываем свежий вместо ошибки."""
        await self._redraw(query.message, 0, note="Список устарел, открыл заново.")

    async def on_delete(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Удаление выбранного события."""
        if not self._authorized(update):
            return

        query = update.callback_query
        _, _, rest = (query.data or "").partition(":")
        token, _, number = rest.partition(":")
        entry = self._entry(token, number)
        known = self._listings.get(token)
        offset = known[0] if known else 0

        if entry is None:
            await query.answer()
            await self._stale(query)
            return

        await query.answer()
        await query.edit_message_text(f"Удаляю «{entry.title}»…")
        results = await self._remove(entry)

        note = f"Удалено: «{entry.title}»" if all("✗" not in line for line in results) else (
            f"«{entry.title}» — " + " · ".join(results)
        )
        await self._redraw(query.message, offset, note=note)

    def _entry(self, token: str, number: str) -> CalendarEntry | None:
        """Событие по номеру из списка. None — если список устарел."""
        known = self._listings.get(token)
        if known is None or not number.isdigit():
            return None
        _, entries = known
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

        if self._icloud is None:
            # iCloud выключен — пары там и не заводилось.
            return results

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

        # Если у события спрошено новое время, это сообщение — ответ на вопрос,
        # а не новый план.
        if await self._apply_answer(update, text):
            return

        link = find_link(text)
        if link is not None:
            await self._from_sheet(update, *link, instruction=strip_link(text))
            return

        if looks_like_sheet(text):
            # Ссылка есть, но идентификатор не вычитывается — обычно её
            # обрезали при копировании. Молча разбирать как текст бесполезно.
            await update.message.reply_text(
                "Вижу ссылку на Google Таблицу, но она неполная — "
                "идентификатор обрезан.\n\n"
                "Открой таблицу в браузере, скопируй адрес из адресной строки "
                "целиком и пришли ещё раз."
            )
            return

        await self._process(update, text)

    async def _from_sheet(
        self, update: Update, sheet_id: str, gid: str, instruction: str
    ) -> None:
        """Ссылка на Google Таблицу: читаем её и предлагаем найденное."""
        await self._typing(update)
        notice = await update.message.reply_text("Открываю таблицу…")

        try:
            text, column = await asyncio.to_thread(
                self._sheets.read, sheet_id, gid, instruction
            )
        except SheetsError as exc:
            logger.warning("Таблица не прочитана: %s", exc)
            await _drop(notice)
            await update.message.reply_text(f"Не смог прочитать таблицу: {exc}")
            return

        logger.info("Таблица прочитана, %d символов", len(text))
        await _drop(notice)
        await self._propose(update, text, instruction, column)

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

        # Подпись к файлу — это просьба: «добавь расписание 11е инж».
        instruction = (message.caption or "").strip()
        column = ""
        try:
            if kind == "image":
                text = await self._image_reader.read(data, mime_type)
            else:
                text, column = await asyncio.to_thread(
                    extract_text, filename, data, instruction
                )
        except (DocumentError, VisionError) as exc:
            logger.warning("Файл %r не прочитан: %s", filename, exc)
            await message.reply_text(f"Не смог прочитать файл: {exc}")
            return

        logger.info("Файл %r прочитан, %d символов", filename, len(text))
        await self._propose(update, text, instruction, column)

    async def _propose(
        self, update: Update, text: str, instruction: str = "", column: str = ""
    ) -> None:
        """Разбирает текст и показывает список, не записывая ничего сразу.

        Извлечение из файла ошибается чаще, чем разбор короткого сообщения,
        поэтому здесь всегда спрашиваем подтверждение.
        """
        await self._typing(update)

        notice = None
        progress = None
        if is_large(text):
            parts = len(split_into_chunks(text))
            about = f" (столбец «{column}»)" if column else ""
            notice = await update.message.reply_text(
                f"Расписание большое{about}: разбираю {_plural_parts(parts)}…"
            )
            progress = _progress_reporter(notice, column)

        try:
            events = await self._parser.parse(
                text, instruction=instruction, on_progress=progress
            )
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
        """Пишет событие в календари параллельно.

        Клиенты календарей блокирующие, поэтому уходят в потоки. Отказ
        одного не отменяет запись в другой — частичный успех виден в
        ответе (контракт A.2.6). iCloud может быть выключен: тогда
        остаётся только Google.
        """
        writes = [asyncio.to_thread(self._google.save, event)]
        if self._icloud is not None:
            writes.append(asyncio.to_thread(self._icloud.save, event))
        return list(await asyncio.gather(*writes))

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


def _progress_reporter(notice, column: str):
    """Обновляет уведомление по мере разбора частей."""
    about = f" (столбец «{column}»)" if column else ""

    async def report(done: int, total: int) -> None:
        try:
            await notice.edit_text(f"Разбираю расписание{about}: {done} из {total}…")
        except TelegramError:
            # Правка уведомления — мелочь, ради неё ничего не ломаем.
            pass

    return report


def _plural_parts(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return f"{count} часть"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return f"{count} части"
    return f"{count} частей"


def _day_name(offset: int, day_start: datetime) -> str:
    """«Сегодня», «Завтра» или название дня недели."""
    if offset == 0:
        return "Сегодня"
    if offset == 1:
        return "Завтра"
    if offset == -1:
        return "Вчера"
    return WEEKDAYS[day_start.weekday()]


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


async def clear_webhook(bot, *, drop_pending: bool) -> str | None:
    """Снимает вебхук с токена, если он там стоит.

    Возвращает адрес снятого вебхука или None, если вебхука не было.
    Ошибки Telegram пробрасывает: насколько они важны, решает вызывающий.
    """
    info = await bot.get_webhook_info()
    if not info.url:
        return None
    await bot.delete_webhook(drop_pending_updates=drop_pending)
    return info.url


async def _recover_from_conflict(bot) -> None:
    """Пробует вернуть поллинг к жизни после Conflict.

    Вебхук могут поставить на токен уже после старта бота — тогда процесс
    жив, исходящие сообщения уходят (ежедневный вопрос приходит вовремя),
    а входящие не видны вовсе. Снимаем вебхук на ходу, не дожидаясь
    перезапуска. Накопленное не выбрасываем: там лежат сообщения, которые
    пользователь уже отправил.
    """
    global _last_conflict_fix

    now = monotonic()
    # Conflict повторяется в каждом цикле поллинга, поэтому в Telegram
    # ходим не чаще раза в минуту.
    if now - _last_conflict_fix < CONFLICT_RETRY_SECONDS:
        return
    _last_conflict_fix = now

    try:
        url = await clear_webhook(bot, drop_pending=False)
    except TelegramError as exc:
        logger.warning("Не удалось проверить вебхук: %s", exc)
        return

    if url is None:
        logger.error(
            "Вебхука на токене нет — значит апдейты забирает второй экземпляр "
            "бота с тем же токеном. Останови лишний."
        )
        return

    logger.warning("Вебхук %s появился после старта — снял, поллинг оживёт", url)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Последний рубеж: логируем всё, что не поймали хендлеры.

    Процесс при этом живёт — Restart=always в systemd остаётся страховкой,
    а не штатным способом пережить ошибку (контракт A.2.5).
    """
    # Conflict прилетает из цикла поллинга на каждый запрос к Telegram, то
    # есть несколько раз в секунду. Полная трассировка забила бы лог и
    # спрятала причину, а причина всегда одна из двух и названа в тексте.
    if isinstance(context.error, Conflict):
        logger.error(
            "Telegram не отдаёт апдейты: %s. Причина — либо вебхук на этом "
            "токене, либо второй запущенный экземпляр бота.",
            context.error,
        )
        await _recover_from_conflict(context.bot)
        return

    logger.exception("Необработанная ошибка", exc_info=context.error)

    message = getattr(update, "message", None)
    if message is None:
        return
    try:
        await message.reply_text("Что-то пошло не так. Подробности в логах сервера.")
    except TelegramError:
        pass
