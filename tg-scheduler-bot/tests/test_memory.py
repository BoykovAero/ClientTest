"""Память бота: разговор, заметки и переживание перезапуска."""

from __future__ import annotations

from types import SimpleNamespace
from zoneinfo import ZoneInfo

from bot.memory import MAX_STORED_CHARS, PROMPT_TURNS, TURN_LIMIT, Memory
from bot.telegram_bot import SchedulerBot

MSK = ZoneInfo("Europe/Moscow")
ME = 1


# ─── разговор ───────────────────────────────────────────────────────────


def test_hod_razgovora_pomnitsya():
    memory = Memory()
    memory.remember(ME, "в 15 зал", "записал: зал")

    turn = memory.recent(ME)[-1]
    assert (turn.you, turn.bot) == ("в 15 зал", "записал: зал")


def test_chuzhaya_pamyat_ne_smeshivaetsya():
    memory = Memory()
    memory.remember(ME, "мой план", "ответ")
    assert memory.recent(999) == []


def test_staroe_vytesnyaetsya():
    """Иначе файл растёт без предела, а модели столько и не нужно."""
    memory = Memory()
    for number in range(TURN_LIMIT + 20):
        memory.remember(ME, f"дело {number}", "ага")

    turns = memory.recent(ME, limit=TURN_LIMIT + 20)
    assert len(turns) == TURN_LIMIT
    assert turns[-1].you == f"дело {TURN_LIMIT + 19}"


def test_raspisanie_tselikom_v_pamyat_ne_lezet():
    memory = Memory()
    memory.remember(ME, "у" * 5000, "ответ")
    assert len(memory.recent(ME)[-1].you) <= MAX_STORED_CHARS + 1


# ─── заметки ────────────────────────────────────────────────────────────


def test_zametka_hranitsya_i_ne_dubliruetsya():
    memory = Memory()
    memory.note(ME, "латинский по вторникам")
    memory.note(ME, "латинский по вторникам")
    assert memory.notes(ME) == ["латинский по вторникам"]


def test_zabyvanie_chistit_vsyo():
    memory = Memory()
    memory.remember(ME, "что-то", "ответ")
    memory.note(ME, "заметка")

    memory.forget(ME)

    assert memory.recent(ME) == []
    assert memory.notes(ME) == []


# ─── подсказка модели ───────────────────────────────────────────────────


def test_podskazka_soderzhit_zametki_i_razgovor():
    memory = Memory()
    memory.note(ME, "школково — это учёба")
    memory.remember(ME, "внеси расписание 11е инж", "нашёл 40 дел")

    prompt = memory.as_prompt(ME)

    assert "школково — это учёба" in prompt
    assert "11е инж" in prompt


def test_pustaya_pamyat_nichego_ne_dobavlyaet():
    assert Memory().as_prompt(ME) == ""


def test_v_podskazku_idyot_tolko_nedavnee():
    memory = Memory()
    for number in range(PROMPT_TURNS + 5):
        memory.remember(ME, f"дело {number}", "ага")

    prompt = memory.as_prompt(ME)

    assert "дело 0" not in prompt
    assert f"дело {PROMPT_TURNS + 4}" in prompt


# ─── диск ───────────────────────────────────────────────────────────────


def test_pamyat_perezhivaet_perezapusk(tmp_path):
    """Railway перезапускает бота на каждом деплое — память в ОЗУ не живёт."""
    path = tmp_path / "внутри" / "memory.json"
    memory = Memory(path)
    memory.remember(ME, "в 15 зал", "записал")
    memory.note(ME, "сколково — работа")

    zanovo = Memory(path)

    assert zanovo.recent(ME)[-1].you == "в 15 зал"
    assert zanovo.notes(ME) == ["сколково — работа"]


def test_bityy_fayl_ne_ronyaet_bota(tmp_path):
    path = tmp_path / "memory.json"
    path.write_text("{не json", encoding="utf-8")

    memory = Memory(path)
    memory.remember(ME, "в 15 зал", "записал")

    assert memory.recent(ME)[-1].you == "в 15 зал"


def test_nezapisyvaemyy_katalog_ne_ronyaet_bota(tmp_path):
    ne_katalog = tmp_path / "файл"
    ne_katalog.write_text("я не каталог", encoding="utf-8")

    memory = Memory(ne_katalog / "memory.json")
    memory.remember(ME, "в 15 зал", "записал")

    # Разговор идёт дальше, просто не переживёт перезапуск.
    assert memory.recent(ME)[-1].you == "в 15 зал"


# ─── бот пользуется памятью ─────────────────────────────────────────────


class RecordingParser:
    def __init__(self) -> None:
        self.memories: list[str] = []

    async def parse(self, text, instruction="", on_progress=None, memory=""):
        self.memories.append(memory)
        return []


class FakeChat:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, text, **kwargs):
        self.sent.append(text)

    async def send_action(self, *args, **kwargs):
        return None


def make_update(chat: FakeChat) -> SimpleNamespace:
    return SimpleNamespace(
        message=SimpleNamespace(chat=chat),
        effective_chat=chat,
        effective_user=SimpleNamespace(id=ME),
    )


def make_bot(parser, memory) -> SchedulerBot:
    return SchedulerBot(
        config=SimpleNamespace(timezone=MSK, allowed_user_id=ME),
        parser=parser,
        transcriber=object(),
        image_reader=object(),
        openai_client=object(),
        google=object(),
        icloud=None,
        memory=memory,
    )


async def test_bot_otdayot_pamyat_modeli():
    memory = Memory()
    memory.remember(ME, "внеси расписание 11е инж", "нашёл 40 дел")
    parser = RecordingParser()

    await make_bot(parser, memory)._process(make_update(FakeChat()), "нет, по дням")

    assert "11е инж" in parser.memories[-1]


async def test_bot_zapominaet_svoy_otvet():
    memory = Memory()
    chat = FakeChat()

    await make_bot(RecordingParser(), memory)._process(make_update(chat), "привет")

    turn = memory.recent(ME)[-1]
    assert turn.you == "привет"
    assert turn.bot == chat.sent[-1]
