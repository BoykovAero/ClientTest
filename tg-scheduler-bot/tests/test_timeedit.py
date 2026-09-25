"""Тесты разбора нового времени и правки готового VEVENT (сеть не нужна)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from bot.timeedit import TimeEditError, parse_new_time, retime_ics

MSK = ZoneInfo("Europe/Moscow")
TODAY = date(2026, 9, 25)


def moment(hour: int, minute: int = 0, day: int = 25) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=MSK)


def move(text: str, start=None, end=None, today: date = TODAY):
    return parse_new_time(text, start or moment(19), end or moment(19, 30), today)


# ─── время внутри того же дня ───────────────────────────────────────────


def test_odno_vremya_sdvigaet_nachalo_sohranyaya_dlitelnost():
    assert move("1500") == (moment(15), moment(15, 30))


def test_dvuchasovoe_delo_ostayotsya_dvuchasovym():
    start, end = move("9:00", moment(19), moment(21))
    assert end - start == timedelta(hours=2)


def test_diapazon_zadayot_oba_kontsa():
    assert move("1500-1620") == (moment(15), moment(16, 20))


@pytest.mark.parametrize("text", ["15:00-16:00", "15.00-16.00", "1500 - 1600", "1500–1600"])
def test_raznye_napisaniya_vremeni(text):
    assert move(text) == (moment(15), moment(16))


# ─── перенос на другой день ─────────────────────────────────────────────


def test_zavtra_perenosit_den_ne_trogaya_chasy():
    assert move("завтра") == (moment(19, 0, 26), moment(19, 30, 26))


def test_data_tochkoy_perenosit_den():
    assert move("28.09") == (moment(19, 0, 28), moment(19, 30, 28))


def test_data_i_vremya_vmeste():
    assert move("завтра 1500") == (moment(15, 0, 26), moment(15, 30, 26))


def test_data_i_diapazon_vmeste():
    assert move("28.09 1500-1600") == (moment(15, 0, 28), moment(16, 0, 28))


def test_god_beryotsya_blizhayshiy_podhodyashchiy():
    """«02.01» в сентябре — это январь следующего года, а не прошедший."""
    start, _ = move("02.01")
    assert start.date() == date(2027, 1, 2)


def test_yavno_ukazannyy_god_uvazhaetsya():
    start, _ = move("02.01.2028")
    assert start.date() == date(2028, 1, 2)


def test_bez_daty_ostayomsya_v_dne_sobytiya():
    """Смена времени двигает событие внутри его дня, а не тащит на сегодня."""
    old_start = datetime(2026, 12, 31, 19, 0, tzinfo=MSK)
    start, _ = move("1500", old_start, old_start + timedelta(minutes=30))
    assert start.date() == old_start.date()


# ─── отказы ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", ["", "потом", "как-нибудь", "25:00", "15:99", "32.13"])
def test_chepuha_otvergaetsya(text):
    with pytest.raises(TimeEditError):
        move(text)


def test_konets_ranshe_nachala_otvergaetsya():
    with pytest.raises(TimeEditError):
        move("1600-1500")


# ─── правка готового VEVENT ─────────────────────────────────────────────

ICS = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:abc123\r\n"
    "DTSTAMP:20260925T060000Z\r\n"
    "DTSTART:20260925T160000Z\r\n"
    "DTEND:20260925T163000Z\r\n"
    "SUMMARY:Сколково\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


def test_retime_menyaet_tolko_vremya():
    result = retime_ics(ICS, moment(15), moment(16))
    assert "DTSTART:20260925T120000Z" in result
    assert "DTEND:20260925T130000Z" in result
    # Личность события сохраняется: по UID его находит пара в Google.
    assert "UID:abc123" in result
    assert "SUMMARY:Сколково" in result


def test_retime_ne_plodit_lishnih_strok():
    result = retime_ics(ICS, moment(15), moment(16))
    assert result.count("DTSTART") == 1
    assert result.count("DTEND") == 1
    assert len(result.splitlines()) == len(ICS.splitlines())


def test_retime_perepisyvaet_i_stroku_s_parametrami():
    """DTSTART;TZID=... тоже наша цель, иначе останутся два начала."""
    text = ICS.replace("DTSTART:20260925T160000Z", "DTSTART;TZID=Europe/Moscow:20260925T190000")
    result = retime_ics(text, moment(15), moment(16))
    assert "TZID" not in result
    assert "DTSTART:20260925T120000Z" in result


def test_bez_dtstart_oshibka():
    with pytest.raises(TimeEditError):
        retime_ics(ICS.replace("DTSTART:20260925T160000Z\r\n", ""), moment(15), moment(16))
