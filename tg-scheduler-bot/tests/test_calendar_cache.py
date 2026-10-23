"""Привязка «направление → календарь» переживает сбой сети при старте.

Если ensure_calendars не прошёл (оборвалась связь), бот должен брать
календари направлений из сохранённой привязки, а не сваливать всё в
основной календарь — иначе олимпиады и дела уходят не туда, а сводка их
не находит.
"""

from __future__ import annotations


from bot.calendars.google import GoogleCalendar
from bot.categories import STUDY

MSK = "Europe/Moscow"
FAKE_SA = {"client_email": "bot@p.iam.gserviceaccount.com", "private_key": "x"}
UCHEBA = "803508...@group.calendar.google.com"


def make(cache):
    return GoogleCalendar(
        calendar_id="me@gmail.com",
        timezone_name=MSK,
        service_account_info=FAKE_SA,
        category_cache=cache,
    )


def test_bez_privyazki_vsyo_v_osnovnoy():
    cal = make(None)
    assert cal.calendar_for(STUDY) == "me@gmail.com"


def test_privyazka_sohranyaetsya_i_chitaetsya(tmp_path):
    cache = tmp_path / "state" / "calendars.json"
    first = make(cache)
    first._category_calendars = {STUDY: UCHEBA}
    first._save_category_cache()

    # Новый процесс (перезапуск): ensure не вызывали, но привязка на месте.
    second = make(cache)
    assert second.calendar_for(STUDY) == UCHEBA


def test_sbita_privyazka_ne_ronyaet(tmp_path):
    cache = tmp_path / "calendars.json"
    cache.write_text("{битый json", encoding="utf-8")
    cal = make(cache)
    # Нечитаемый кэш — просто работаем без привязки, не падаем.
    assert cal.calendar_for(STUDY) == "me@gmail.com"
