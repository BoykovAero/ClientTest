"""Ежедневная сводка по «Учёбе»: что сегодня по олимпиадам и ИД.

Бот ничего не меняет в календаре — только читает направление «Учёба» за
сегодня и шлёт список одним сообщением.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from bot.calendars.base import CalendarEntry, CalendarError
from bot.categories import STUDY
from bot.telegram_bot import SchedulerBot

MSK = ZoneInfo("Europe/Moscow")


def moment(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 22, hour, minute, tzinfo=MSK)


ROUND = CalendarEntry(
    event_id="e1", when="10:00", title="Физтех (физика) — финал",
    start=moment(10), end=moment(13),
)
WINDOW = CalendarEntry(event_id="e2", when="весь день", title="Innopolis Open — регистрация")


class FakeGoogle:
    def __init__(self, entries=(), fail=False) -> None:
        self._entries = list(entries)
        self._fail = fail
        self.asked: list = []

    def list_category_day(self, category, day_start):
        self.asked.append((category, day_start))
        if self._fail:
            raise CalendarError("403: нет доступа")
        return list(self._entries)


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


def make_bot(google) -> SchedulerBot:
    return SchedulerBot(
        config=SimpleNamespace(timezone=MSK, allowed_user_id=777),
        parser=object(), transcriber=object(), image_reader=object(),
        openai_client=object(), google=google, icloud=None,
    )


def context(bot: FakeBot) -> SimpleNamespace:
    return SimpleNamespace(bot=bot)


# ─── формат ─────────────────────────────────────────────────────────────


def test_svodka_odnim_spiskom_bez_deleniya():
    bot = make_bot(FakeGoogle())
    text = bot._digest_text([ROUND, WINDOW], moment(0))
    assert "Физтех (физика) — финал" in text
    assert "Innopolis Open — регистрация" in text
    # Ровно один заголовок, без секций «олимпиады»/«ИД» по отдельности.
    assert text.count("•") == 2


def test_pustoy_den_tak_i_pishetsya():
    text = make_bot(FakeGoogle())._digest_text([], moment(0))
    assert "ничего не запланировано" in text


# ─── отправка ───────────────────────────────────────────────────────────


async def test_svodka_chitaet_uchyobu_i_shlyot_vladeltsu():
    google = FakeGoogle([ROUND])
    bot = make_bot(google)
    fake = FakeBot()

    await bot.daily_digest(context(fake))

    assert google.asked[0][0] == STUDY
    assert len(fake.sent) == 1
    chat_id, text = fake.sent[0]
    assert chat_id == 777
    assert "Физтех" in text


async def test_pustoy_den_vsyo_ravno_otpravlyaetsya():
    """Сводка приходит каждый день — так видно, что бот жив."""
    fake = FakeBot()
    await make_bot(FakeGoogle([])).daily_digest(context(fake))
    assert len(fake.sent) == 1
    assert "ничего" in fake.sent[0][1]


async def test_otkaz_kalendarya_ne_ronyaet_i_ne_shlyot():
    fake = FakeBot()
    await make_bot(FakeGoogle(fail=True)).daily_digest(context(fake))
    assert fake.sent == []


async def test_nichego_ne_menyaet_v_kalendare():
    """У сводки нет ни create, ни patch — только чтение одного дня."""
    google = FakeGoogle([ROUND, WINDOW])
    await make_bot(google).daily_digest(context(FakeBot()))
    # Единственное обращение к календарю — чтение «Учёбы» за день.
    assert len(google.asked) == 1
