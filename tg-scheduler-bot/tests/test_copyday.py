"""Копирование дел с одного дня на другие: разбор просьбы и сама работа."""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from bot.calendars.base import CalendarEntry, CalendarError, SaveResult
from bot.copyday import CopyError, matches, parse_copy
from bot.telegram_bot import SchedulerBot

MSK = ZoneInfo("Europe/Moscow")
# Четверг: до понедельника назад четыре дня, до среды вперёд шесть.
TODAY = date(2026, 10, 1)
ME = 1


def ask(text: str):
    return parse_copy(text, TODAY)


# ─── разбор просьбы ─────────────────────────────────────────────────────


def test_den_i_nazvanie_uznayutsya():
    got = ask("скопируй математику с понедельника на среду")
    assert got.title == "математику"
    assert got.source == date(2026, 9, 28)
    assert got.targets == (date(2026, 10, 7),)


def test_den_bez_predloga_tozhe_schitaetsya():
    """«Скопируй понедельник на среду» — предлога нет, а день назван."""
    got = ask("скопируй весь понедельник на среду")
    assert got.title == ""
    assert got.source == date(2026, 9, 28)


def test_istochnik_v_proshlom_a_cel_v_budushchem():
    """Копируют с прошедшего дня на предстоящий, а не наоборот."""
    got = ask("повтори физику со вторника на завтра")
    assert got.source == date(2026, 9, 29)
    assert got.targets == (date(2026, 10, 2),)


def test_neskolko_dney_srazu():
    got = ask("скопируй математику с понедельника на среду и пятницу")
    assert got.targets == (date(2026, 10, 2), date(2026, 10, 7))


def test_proshlaya_nedelya_otschityvaetsya_nazad():
    got = ask("скопируй с прошлой пятницы английский на завтра")
    assert got.source == date(2026, 9, 25)


def test_yavnaya_data_ponimaetsya():
    got = ask("продублируй химию с 28.09 на 05.10")
    assert (got.source, got.targets) == (date(2026, 9, 28), (date(2026, 10, 5),))


def test_obychnyy_plan_ne_schitaetsya_kopirovaniem():
    """«Повтори билеты в 19» — это дело на вечер, а не копирование дня."""
    assert ask("повтори билеты в 19") is None
    assert ask("в 15 зал") is None


def test_pro_kopirovanie_no_bez_dnya_eto_oshibka():
    with pytest.raises(CopyError):
        ask("скопируй математику с понедельника")


# ─── поиск дела по названию ─────────────────────────────────────────────


def test_padezh_ne_meshaet():
    assert matches("Математика Флоринская Т.Р.", "математику")


def test_ishchetsya_po_vsem_slovam():
    assert matches("Подготовка к ЕГЭ по математике", "подготовку к егэ по математике")
    assert not matches("Математика Флоринская", "подготовку к егэ по математике")


def test_shozhee_nazvanie_ne_lovitsya():
    """Физика и физкультура — разные дела, и путать их нельзя."""
    assert not matches("Физкультура Акопян", "физику")


def test_pustoy_zapros_beryot_vsyo():
    assert matches("Что угодно", "")


# ─── работа бота ────────────────────────────────────────────────────────


def moment(hour: int, minute: int = 0, day: int = 28, month: int = 9):
    return datetime(2026, month, day, hour, minute, tzinfo=MSK)


MATH = CalendarEntry(
    event_id="e1", when="09:00", title="Математика Флоринская Т.Р.", uid="u1",
    start=moment(9), end=moment(9, 40), notes="кабинет 12", category="Учёба",
)
CHEM = CalendarEntry(
    event_id="e2", when="11:40", title="Химия Волков А.А.", uid="u2",
    start=moment(11, 40), end=moment(12, 20),
)
HOLIDAY = CalendarEntry(event_id="e3", when="весь день", title="Каникулы")


class FakeGoogle:
    def __init__(self, entries=(), fail=False) -> None:
        self._entries = list(entries)
        self._fail = fail
        self.asked: list[datetime] = []
        self.saved: list = []

    def list_day(self, day_start):
        if self._fail:
            raise CalendarError("403: нет доступа")
        self.asked.append(day_start)
        return list(self._entries)

    def save(self, event):
        self.saved.append(event)
        return SaveResult("Google", True, "")


class FakeChat:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, text, **kwargs):
        self.sent.append(text)

    async def send_action(self, *args, **kwargs):
        return None


def make_bot(google) -> SchedulerBot:
    return SchedulerBot(
        config=SimpleNamespace(timezone=MSK, allowed_user_id=ME),
        parser=object(), transcriber=object(), image_reader=object(),
        openai_client=object(), google=google, icloud=None,
    )


def update(chat: FakeChat):
    return SimpleNamespace(
        message=SimpleNamespace(chat=chat),
        effective_chat=chat,
        effective_user=SimpleNamespace(id=ME),
    )


async def test_delo_kopiruetsya_s_tem_zhe_vremenem():
    google = FakeGoogle([MATH, CHEM])
    chat = FakeChat()

    handled = await make_bot(google)._apply_copy(
        update(chat), "скопируй математику с понедельника на среду"
    )

    assert handled
    assert len(google.saved) == 1
    copied = google.saved[0]
    assert copied.title == "Математика Флоринская Т.Р."
    assert (copied.start.hour, copied.start.minute) == (9, 0)
    assert copied.end - copied.start == MATH.end - MATH.start


async def test_zametka_i_napravlenie_perenosyatsya():
    google = FakeGoogle([MATH])
    await make_bot(google)._apply_copy(
        update(FakeChat()), "скопируй математику с понедельника на среду"
    )
    assert google.saved[0].notes == "кабинет 12"
    assert google.saved[0].category == "Учёба"


async def test_na_neskolko_dney_po_kopii_na_kazhdyy():
    google = FakeGoogle([MATH])
    await make_bot(google)._apply_copy(
        update(FakeChat()), "скопируй математику с понедельника на среду и пятницу"
    )
    assert {event.start.date() for event in google.saved} == {
        date(2026, 10, 2), date(2026, 10, 7)
    }


async def test_ves_den_kopiruetsya_tselikom():
    google = FakeGoogle([MATH, CHEM])
    await make_bot(google)._apply_copy(
        update(FakeChat()), "скопируй весь понедельник на среду"
    )
    assert len(google.saved) == 2


async def test_sobytie_na_ves_den_propuskaetsya():
    """У него нет часов: переносить нечего, а молча терять нельзя."""
    google = FakeGoogle([MATH, HOLIDAY])
    chat = FakeChat()

    await make_bot(google)._apply_copy(update(chat), "скопируй понедельник на среду")

    assert len(google.saved) == 1
    assert "весь день" in chat.sent[-1]


async def test_nenaydennoe_delo_pokazyvaet_chto_est():
    google = FakeGoogle([MATH, CHEM])
    chat = FakeChat()

    await make_bot(google)._apply_copy(
        update(chat), "скопируй литературу с понедельника на среду"
    )

    assert google.saved == []
    assert "Химия Волков А.А." in chat.sent[-1]


async def test_pustoy_den_tak_i_govoritsya():
    chat = FakeChat()
    await make_bot(FakeGoogle([]))._apply_copy(
        update(chat), "скопируй математику с понедельника на среду"
    )
    assert "ничего не записано" in chat.sent[-1]


async def test_otkaz_kalendarya_ne_ronyaet_bota():
    chat = FakeChat()
    handled = await make_bot(FakeGoogle(fail=True))._apply_copy(
        update(chat), "скопируй математику с понедельника на среду"
    )
    assert handled
    assert "403" in chat.sent[-1]


async def test_obychnoe_soobshchenie_prohodit_mimo():
    google = FakeGoogle([MATH])
    assert await make_bot(google)._apply_copy(update(FakeChat()), "в 15 зал") is False
    assert google.asked == []
