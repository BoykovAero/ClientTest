"""Выбор столбца таблицы кнопкой: вопрос, ответ и разбор выбранного.

Главное здесь — что после нажатия кнопки бот вообще отвечает. У нажатия
нет update.message, и обращение к нему роняло обработчик молча.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from bot.calendars.base import Event
from bot.telegram_bot import SchedulerBot

MSK = ZoneInfo("Europe/Moscow")

COLUMNS = ["11е инж", "11е УТ", "11е ИТ"]


class FakeChat:
    """Чат помнит всё отправленное — по нему и проверяем ответ."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.markups: list = []

    async def send_message(self, text, reply_markup=None, **kwargs):
        self.sent.append(text)
        self.markups.append(reply_markup)
        return FakeMessage(self)

    async def send_action(self, *args, **kwargs):
        return None


class FakeMessage:
    def __init__(self, chat: FakeChat) -> None:
        self.chat = chat
        self.text = ""
        self.markup = None
        self.deleted = False

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.chat.sent.append(text)
        self.chat.markups.append(reply_markup)
        return FakeMessage(self.chat)

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self.text = text
        self.markup = reply_markup

    async def delete(self):
        self.deleted = True


class FakeQuery:
    def __init__(self, data: str, chat: FakeChat) -> None:
        self.data = data
        self.message = FakeMessage(chat)
        self.answered = False

    async def answer(self, *args, **kwargs):
        self.answered = True

    async def edit_message_text(self, text, reply_markup=None, **kwargs):
        self.message.text = text


class FakeSheets:
    """Отдаёт текст того столбца, который у неё попросили."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def read(self, sheet_id, gid="", instruction=""):
        self.asked.append(instruction)
        return f"понедельник 09:00 матан ({instruction})", instruction


class FakeParser:
    def __init__(self) -> None:
        self.seen: list[str] = []

    async def parse(self, text, instruction="", on_progress=None):
        self.seen.append(text)
        return [
            Event(
                title="матан",
                start=datetime(2026, 9, 28, 9, tzinfo=MSK),
                end=datetime(2026, 9, 28, 10, tzinfo=MSK),
            )
        ]


def make_bot(sheets=None, parser=None) -> SchedulerBot:
    bot = SchedulerBot(
        config=SimpleNamespace(timezone=MSK, allowed_user_id=1),
        parser=parser or FakeParser(),
        transcriber=object(),
        image_reader=object(),
        openai_client=object(),
        google=object(),
        icloud=None,
    )
    bot._sheets = sheets or FakeSheets()
    return bot


def message_update(chat: FakeChat) -> SimpleNamespace:
    return SimpleNamespace(
        message=FakeMessage(chat),
        callback_query=None,
        effective_chat=chat,
        effective_user=SimpleNamespace(id=1),
    )


def press(chat: FakeChat, data: str) -> SimpleNamespace:
    """Нажатие кнопки: message у такого апдейта нет — только callback_query."""
    return SimpleNamespace(
        message=None,
        callback_query=FakeQuery(data, chat),
        effective_chat=chat,
        effective_user=SimpleNamespace(id=1),
    )


async def ask(bot: SchedulerBot, chat: FakeChat) -> str:
    await bot._ask_column(
        message_update(chat), COLUMNS, ("sheet", "sheet-id", ""), True
    )
    return chat.markups[-1].inline_keyboard[0][0].callback_data


# ─── вопрос ─────────────────────────────────────────────────────────────


async def test_vopros_pokazyvaet_vse_varianty():
    chat = FakeChat()
    await make_bot()._ask_column(
        message_update(chat), COLUMNS, ("sheet", "sheet-id", ""), True
    )
    shown = [b.text for row in chat.markups[-1].inline_keyboard for b in row]
    assert shown == COLUMNS


# ─── ответ ──────────────────────────────────────────────────────────────


async def test_posle_vybora_bot_otvechaet():
    """Ради этого тест и написан: раньше здесь была тишина."""
    bot, chat = make_bot(), FakeChat()
    data = await ask(bot, chat)
    before = len(chat.sent)

    await bot.on_column(press(chat, data), None)

    assert len(chat.sent) > before
    assert "матан" in chat.sent[-1]


async def test_vybrannyy_stolbec_uhodit_v_istochnik():
    sheets = FakeSheets()
    bot, chat = make_bot(sheets), FakeChat()
    data = await ask(bot, chat)

    await bot.on_column(press(chat, data), None)

    assert sheets.asked == ["11е инж"]
    assert "11е инж" in bot._parser.seen[-1]


async def test_ustarevshuyu_knopku_ne_molchat():
    bot, chat = make_bot(), FakeChat()
    update = press(chat, "col:нетакого:0")

    await bot.on_column(update, None)

    assert "устарел" in update.callback_query.message.text


async def test_chuzhoy_nazhatie_ignoriruem():
    bot, chat = make_bot(), FakeChat()
    data = await ask(bot, chat)
    update = press(chat, data)
    update.effective_user = SimpleNamespace(id=999)

    await bot.on_column(update, None)

    assert bot._sheets.asked == []
