"""Выбор столбца таблицы кнопкой: вопрос, ответ и разбор выбранного.

Главное здесь — что после нажатия кнопки бот вообще отвечает. У нажатия
нет update.message, и обращение к нему роняло обработчик молча.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from bot.calendars.base import Event
from bot.grid import ColumnChoiceNeeded
from bot.parser import ParseError
from bot.telegram_bot import SchedulerBot

MSK = ZoneInfo("Europe/Moscow")

COLUMNS = ["11е инж", "11е УТ", "11е ИТ"]


class FakeChat:
    """Чат помнит всё отправленное — по нему и проверяем ответ."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.markups: list = []
        self.documents: list[str] = []

    async def send_message(self, text, reply_markup=None, **kwargs):
        self.sent.append(text)
        self.markups.append(reply_markup)
        return FakeMessage(self)

    async def send_document(self, document, filename="", caption="", **kwargs):
        self.documents.append(document.getvalue().decode("utf-8"))
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


class SheetsAsking:
    """Столбец не подошёл — источник просит выбрать заново."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def read(self, sheet_id, gid="", instruction=""):
        self.asked.append(instruction)
        raise ColumnChoiceNeeded(COLUMNS, matched=False)


class FakeParser:
    def __init__(self) -> None:
        self.seen: list[str] = []

    async def parse(self, text, instruction="", on_progress=None, memory=""):
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
    assert shown[: len(COLUMNS)] == COLUMNS


async def test_iz_voprosa_est_vyhod():
    bot, chat = make_bot(), FakeChat()
    data = await ask(bot, chat)
    token = data.split(":")[1]
    update = press(chat, f"col:{token}:cancel")

    await bot.on_column(update, None)

    assert bot._sheets.asked == []
    assert bot._pending_sources == {}
    assert bot._awaiting == {}


async def test_dlinnyy_spisok_zovyot_napisat_nazvanie():
    """В кнопки влезает дюжина, а классов в школе под сотню."""
    chat = FakeChat()
    await make_bot()._ask_column(
        message_update(chat), [f"стлб{i}" for i in range(64)], ("sheet", "s", ""), False
    )
    assert "напиши название" in chat.sent[-1]


# ─── ответ текстом ──────────────────────────────────────────────────────


async def test_nazvanie_tekstom_prinimaetsya_kak_otvet():
    """Столбца нет в кнопках — его пишут сообщением, а не планом на день."""
    bot, chat = make_bot(), FakeChat()
    await ask(bot, chat)

    handled = await bot._apply_answer(message_update(chat), "11е инж")

    assert handled
    assert bot._sheets.asked == ["11е инж"]
    assert "матан" in chat.sent[-1]


async def test_posle_otveta_tekstom_vopros_zakryt():
    """Иначе следующий план съедался бы как название столбца."""
    bot, chat = make_bot(), FakeChat()
    await ask(bot, chat)
    await bot._apply_answer(message_update(chat), "11е инж")

    assert bot._awaiting == {}
    assert await bot._apply_answer(message_update(chat), "в 15:00 зал") is False


async def test_novaya_ssylka_ne_schitaetsya_nazvaniem_stolbca():
    bot, chat = make_bot(), FakeChat()
    await ask(bot, chat)

    handled = await bot._apply_answer(
        message_update(chat), "https://docs.google.com/spreadsheets/d/" + "a" * 25
    )

    assert handled is False
    assert bot._sheets.asked == []


async def test_nesushchestvuyushchiy_stolbec_nazvan_v_otvete():
    bot, chat = make_bot(SheetsAsking()), FakeChat()
    await ask(bot, chat)

    await bot._apply_answer(message_update(chat), "11е физ")

    assert "«11е физ»" in chat.sent[-1]


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


# ─── видно, что прочитано ───────────────────────────────────────────────


class EmptyParser:
    """Разбор прошёл, но дел не нашлось."""

    async def parse(self, text, instruction="", on_progress=None, memory=""):
        return []


class FailingParser:
    async def parse(self, text, instruction="", on_progress=None, memory=""):
        raise ParseError("модель вернула не JSON")


async def test_pustoy_razbor_pokazyvaet_prochitannoe():
    """Иначе о причине можно только гадать: текст таблицы никто не видит."""
    bot, chat = make_bot(parser=EmptyParser()), FakeChat()
    await ask(bot, chat)

    await bot._apply_answer(message_update(chat), "11е инж")

    assert chat.documents == ["понедельник 09:00 матан (11е инж)"]


async def test_sboy_razbora_pokazyvaet_prochitannoe():
    bot, chat = make_bot(parser=FailingParser()), FakeChat()
    await ask(bot, chat)

    await bot._apply_answer(message_update(chat), "11е инж")

    assert chat.documents == ["понедельник 09:00 матан (11е инж)"]


async def test_udachnyy_razbor_nichego_lishnego_ne_shlyot():
    bot, chat = make_bot(), FakeChat()
    await ask(bot, chat)

    await bot._apply_answer(message_update(chat), "11е инж")

    assert chat.documents == []
