"""Тесты модели события и сборки iCalendar."""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bot.calendars.base import Event
from bot.calendars.icloud import _escape, _fold, build_ics

MSK = ZoneInfo("Europe/Moscow")


def make_event(**overrides) -> Event:
    defaults = dict(
        title="Созвон с командой",
        start=datetime(2026, 9, 16, 15, 0, tzinfo=MSK),
        end=datetime(2026, 9, 16, 16, 0, tzinfo=MSK),
        all_day=False,
        notes="",
    )
    defaults.update(overrides)
    return Event(**defaults)


class TestEventValidation:
    def test_rejects_naive_datetime(self):
        with pytest.raises(ValueError, match="тайм-зоной"):
            make_event(start=datetime(2026, 9, 16, 15, 0))

    def test_rejects_empty_title(self):
        with pytest.raises(ValueError, match="заголовок"):
            make_event(title="   ")

    def test_rejects_end_before_start(self):
        with pytest.raises(ValueError, match="раньше начала"):
            make_event(end=datetime(2026, 9, 16, 14, 0, tzinfo=MSK))


class TestEventUid:
    def test_is_deterministic(self):
        assert make_event().uid == make_event().uid

    def test_differs_when_time_differs(self):
        other = make_event(start=datetime(2026, 9, 16, 16, 0, tzinfo=MSK),
                           end=datetime(2026, 9, 16, 17, 0, tzinfo=MSK))
        assert make_event().uid != other.uid

    def test_ignores_title_case(self):
        assert make_event(title="СОЗВОН С КОМАНДОЙ").uid == make_event().uid

    def test_matches_google_id_rules(self):
        """Google принимает только base32hex: символы 0-9a-v, длина 5-1024."""
        uid = make_event().uid
        assert re.fullmatch(r"[0-9a-v]{5,1024}", uid), uid

    def test_marker_embedded_in_description(self):
        event = make_event(notes="в переговорке")
        description = event.description()
        assert "в переговорке" in description
        assert event.marker in description


class TestHumanRange:
    def test_same_day_shows_one_date(self):
        assert make_event().human_range(MSK) == "16.09 15:00–16:00"

    def test_all_day_single_date(self):
        event = make_event(
            start=datetime(2026, 9, 16, tzinfo=MSK),
            end=datetime(2026, 9, 16, tzinfo=MSK),
            all_day=True,
        )
        assert event.human_range(MSK) == "16.09, весь день"


class TestIcsEscaping:
    def test_escapes_special_characters(self):
        assert _escape("а;б,в\\г") == "а\\;б\\,в\\\\г"

    def test_escapes_newlines(self):
        assert _escape("первая\nвторая") == "первая\\nвторая"

    def test_fold_keeps_short_line_intact(self):
        assert _fold("SUMMARY:коротко") == "SUMMARY:коротко"

    def test_fold_splits_long_line(self):
        folded = _fold("SUMMARY:" + "я" * 200)
        assert "\r\n " in folded
        # каждая строка укладывается в 75 октетов
        for line in folded.split("\r\n"):
            assert len(line.encode("utf-8")) <= 75

    def test_fold_does_not_break_utf8(self):
        """Разрыв не должен приходиться на середину многобайтового символа."""
        folded = _fold("DESCRIPTION:" + "ё" * 120)
        rebuilt = folded.replace("\r\n ", "")
        assert rebuilt == "DESCRIPTION:" + "ё" * 120


class TestBuildIcs:
    def test_contains_required_components(self):
        ics = build_ics(make_event())
        for marker in ("BEGIN:VCALENDAR", "BEGIN:VEVENT", "END:VEVENT", "END:VCALENDAR"):
            assert marker in ics

    def test_uid_matches_event(self):
        event = make_event()
        assert f"UID:{event.uid}" in build_ics(event)

    def test_times_converted_to_utc(self):
        """15:00 MSK (UTC+3) -> 12:00Z, без VTIMEZONE."""
        ics = build_ics(make_event())
        assert "DTSTART:20260916T120000Z" in ics
        assert "DTEND:20260916T130000Z" in ics
        assert "VTIMEZONE" not in ics

    def test_all_day_uses_date_values(self):
        event = make_event(
            start=datetime(2026, 9, 16, tzinfo=MSK),
            end=datetime(2026, 9, 16, tzinfo=MSK),
            all_day=True,
        )
        ics = build_ics(event)
        assert "DTSTART;VALUE=DATE:20260916" in ics
        # конец у события на весь день эксклюзивный — следующий день
        assert "DTEND;VALUE=DATE:20260917" in ics

    def test_uses_crlf_line_endings(self):
        ics = build_ics(make_event())
        assert "\r\n" in ics
        assert re.search(r"[^\r]\n", ics) is None
