"""Тесты карточки события: перенос, заметка, удаление (сеть не нужна)."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from bot.calendars.base import CalendarEntry, CalendarError
from bot.telegram_bot import SchedulerBot

MSK = ZoneInfo("Europe/Moscow")


def moment(hour: int, minute: int = 0, day: int = 25) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=MSK)


TIMED = CalendarEntry(
    event_id="ev1",
    when="19:00",
    title="сколково",
    uid="abc123",
    start=moment(19),
    end=moment(19, 30),
)
ALL_DAY = CalendarEntry(event_id="ev2", when="весь день", title="Отпуск", uid="def456")
WITH_NOTES = CalendarEntry(
    event_id="ev3",
    when="19:00",
    title="созвон",
    uid="ghi789",
    start=moment(19),
    end=moment(19, 30),
    notes="в переговорке",
)


class FakeGoogle:
    def __init__(self, fail: bool = False) -> None:
        self.moved: list[tuple] = []
        self.noted: list[tuple] = []
        self._fail = fail

    def move(self, event_id, start, end, calendar_id=""):
        if self._fail:
            raise CalendarError("404: события нет")
        self.moved.append((event_id, start, end))

    def set_notes(self, event_id, notes, calendar_id=""):
        if self._fail:
            raise CalendarError("403: нет доступа")
        self.noted.append((event_id, notes))

    def list_day(self, day_start):
        # После правки бот перерисовывает день — список для этого и нужен.
        return []


class FakeICloud:
    def __init__(self, present: bool = True) -> None:
        self.moved: list[tuple] = []
        self.noted: list[tuple] = []
        self._present = present

    def move_by_uid(self, uid, start, end):
        self.moved.append((uid, start, end))
        return self._present

    def set_notes_by_uid(self, uid, notes):
        self.noted.append((uid, notes))
        return self._present


class FakeMessage:
    def __init__(self) -> None:
        self.text = ""
        self.markup = None
        self.deleted = False

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self.text = text
        self.markup = reply_markup

    async def delete(self):
        self.deleted = True


async def _noop(*args, **kwargs):
    return None


def make_bot(google=None, icloud=None) -> SchedulerBot:
    return SchedulerBot(
        config=SimpleNamespace(timezone=MSK, allowed_user_id=1),
        parser=object(),
        transcriber=object(),
        image_reader=object(),
        openai_client=object(),
        google=google or FakeGoogle(),
        icloud=icloud,
    )


def make_update(text: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        message=FakeMessage(), effective_user=SimpleNamespace(id=1), text=text
    )


# ─── карточка ───────────────────────────────────────────────────────────


def labels(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def test_u_sobytiya_so_vremenem_est_perenos():
    assert "Перенести" in labels(SchedulerBot._card_keyboard(TIMED, "tok", "1"))


def test_u_sobytiya_na_ves_den_perenosa_net():
    """По часам такое событие не двигают — кнопка только сбивала бы с толку."""
    assert "Перенести" not in labels(SchedulerBot._card_keyboard(ALL_DAY, "tok", "1"))


def test_udalenie_i_zametka_est_vsegda():
    for entry in (TIMED, ALL_DAY):
        shown = labels(SchedulerBot._card_keyboard(entry, "tok", "1"))
        assert "Удалить" in shown
        assert any(name.startswith("Заметк") for name in shown)


def test_iz_kartochki_est_vozvrat_k_spisku():
    markup = SchedulerBot._card_keyboard(TIMED, "tok", "1")
    assert markup.inline_keyboard[-1][0].callback_data == "back:tok"


def test_kartochka_pokazyvaet_nyneshnyuyu_zametku():
    assert "в переговорке" in SchedulerBot._card_text(WITH_NOTES)


def test_kartochka_pokazyvaet_vopros():
    assert "Пришли время" in SchedulerBot._card_text(TIMED, prompt="Пришли время")


# ─── перенос ────────────────────────────────────────────────────────────


async def test_perenos_idyot_v_oba_kalendarya():
    google, icloud = FakeGoogle(), FakeICloud()
    results = await make_bot(google, icloud)._move(TIMED, moment(15), moment(16))
    assert google.moved == [("ev1", moment(15), moment(16))]
    assert icloud.moved == [("abc123", moment(15), moment(16))]
    assert results == ["Google ✓", "iCloud ✓"]


async def test_bez_icloud_perenos_tolko_v_google():
    google = FakeGoogle()
    results = await make_bot(google)._move(TIMED, moment(15), moment(16))
    assert results == ["Google ✓"]


async def test_otkaz_google_viden_v_otchyote():
    results = await make_bot(FakeGoogle(fail=True))._move(TIMED, moment(15), moment(16))
    assert results[0].startswith("Google ✗")


# ─── заметка ────────────────────────────────────────────────────────────


async def test_zametka_idyot_v_oba_kalendarya():
    google, icloud = FakeGoogle(), FakeICloud()
    results = await make_bot(google, icloud)._set_notes(TIMED, "взять ноутбук")
    assert google.noted == [("ev1", "взять ноутбук")]
    assert icloud.noted == [("abc123", "взять ноутбук")]
    assert results == ["Google ✓", "iCloud ✓"]


async def test_chuzhoe_sobytie_v_icloud_ne_trogaem():
    """Пары там нет: uid проставляет только бот."""
    icloud = FakeICloud()
    foreign = CalendarEntry(event_id="ev9", when="18:00", title="Чужое", uid="")
    await make_bot(FakeGoogle(), icloud)._set_notes(foreign, "что-то")
    assert icloud.noted == []


async def test_defis_styraet_zametku():
    google = FakeGoogle()
    bot = make_bot(google)
    card = FakeMessage()
    bot._listings["tok"] = (0, [WITH_NOTES])
    bot._awaiting[1] = ("note", "tok", "1", card)

    update = make_update()
    assert await bot._apply_answer(update, "-") is True
    assert google.noted == [("ev3", "")]


async def test_soobshchenie_s_otvetom_udalyaetsya():
    """Чат не должен зарастать: остаётся один живой список."""
    bot = make_bot(FakeGoogle())
    bot._listings["tok"] = (0, [WITH_NOTES])
    bot._awaiting[1] = ("note", "tok", "1", FakeMessage())

    update = make_update()
    await bot._apply_answer(update, "взять ноутбук")
    assert update.message.deleted


async def test_ozhidanie_snimaetsya_posle_otveta():
    bot = make_bot(FakeGoogle())
    bot._listings["tok"] = (0, [WITH_NOTES])
    bot._awaiting[1] = ("note", "tok", "1", FakeMessage())

    await bot._apply_answer(make_update(), "готово")
    assert bot._awaiting == {}


# ─── ответ, которого не ждали ───────────────────────────────────────────


async def test_bez_voprosa_tekst_razbiraetsya_kak_plan():
    bot = make_bot(FakeGoogle())
    assert await bot._apply_answer(make_update(), "1800 сколково") is False


async def test_oshibka_vo_vremeni_ne_snimaet_ozhidanie():
    """Человек просто опечатался — спрашиваем снова, а не молчим."""
    google = FakeGoogle()
    bot = make_bot(google)
    bot._listings["tok"] = (0, [TIMED])
    bot._awaiting[1] = ("time", "tok", "1", FakeMessage())

    assert await bot._apply_answer(make_update(), "когда-нибудь") is True
    assert bot._awaiting != {}
    assert google.moved == []


# ─── направление ────────────────────────────────────────────────────────


class FakeRouting(FakeGoogle):
    """Google, знающий про календари направлений."""

    def __init__(self, fail: bool = False) -> None:
        super().__init__(fail)
        self.relocated: list[tuple] = []

    def move_to_category(self, event_id, source, category):
        if self._fail:
            raise CalendarError("403: нет доступа")
        self.relocated.append((event_id, source, category))
        return f"cal-{category}"


def test_v_kartochke_est_knopka_napravleniya():
    data = [b.callback_data for row in SchedulerBot._card_keyboard(TIMED, "tok", "1").inline_keyboard for b in row]
    assert "cat:tok:1" in data


def test_kartochka_nazyvaet_nyneshnee_napravlenie():
    entry = CalendarEntry(
        event_id="ev1", when="19:00", title="сколково", start=moment(19), end=moment(19, 30),
        category="Работа",
    )
    assert "Работа" in SchedulerBot._card_text(entry)


async def test_smena_napravleniya_perenosit_sobytie():
    google = FakeRouting()
    bot = make_bot(google)
    entry = CalendarEntry(
        event_id="ev1", when="19:00", title="сколково", start=moment(19), end=moment(19, 30),
        calendar_id="cal-main",
    )
    bot._listings["tok"] = (0, [entry])

    query = SimpleNamespace(
        data="setcat:tok:1:1",
        message=FakeMessage(),
        from_user=SimpleNamespace(id=1),
        answer=_noop,
        edit_message_text=_noop,
    )
    await bot.on_set_category(
        SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=1)), None
    )
    assert google.relocated == [("ev1", "cal-main", "Работа")]
