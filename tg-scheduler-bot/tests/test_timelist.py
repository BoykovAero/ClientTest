"""Тесты разбора списка дел без модели."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bot.timelist import parse_time_list

MSK = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 9, 17, 13, 53, tzinfo=MSK)


def parse(text: str, minutes: int = 60):
    return parse_time_list(text, NOW, MSK, minutes)


class TestRealMessage:
    """Ровно тот список, что пришёл от пользователя."""

    TEXT = """1555-1620 установить клод
1800 сколково
1930 физ шк
2045 книга
22 физ шк
2230 хм+отдых
2250 рутины+спать"""

    def test_all_events_parsed(self):
        assert len(parse(self.TEXT)) == 7

    def test_range_line(self):
        first = parse(self.TEXT)[0]
        assert first.title == "Установить клод"
        assert (first.start.hour, first.start.minute) == (15, 55)
        assert (first.end.hour, first.end.minute) == (16, 20)

    def test_single_time_is_the_end(self):
        second = parse(self.TEXT)[1]
        assert second.title == "Сколково"
        assert (second.start.hour, second.start.minute) == (16, 20)
        assert (second.end.hour, second.end.minute) == (18, 0)

    def test_no_overlaps_and_no_gaps(self):
        events = parse(self.TEXT)
        for earlier, later in zip(events, events[1:]):
            assert later.start == earlier.end

    def test_bare_hour_means_whole_hour(self):
        """«22 физ шк» — это 22:00."""
        fifth = parse(self.TEXT)[4]
        assert (fifth.end.hour, fifth.end.minute) == (22, 0)


class TestTimeFormats:
    @pytest.mark.parametrize(
        "line,hour,minute",
        [
            ("9 дело\n10 дело", 9, 0),
            ("930 дело\n10 дело", 9, 30),
            ("9:30 дело\n10 дело", 9, 30),
            ("9.30 дело\n10 дело", 9, 30),
            ("1800 дело\n19 дело", 18, 0),
            ("18:00 дело\n19 дело", 18, 0),
        ],
    )
    def test_accepted_forms(self, line, hour, minute):
        first = parse(line)[0]
        assert (first.end.hour, first.end.minute) == (hour, minute)

    @pytest.mark.parametrize("dash", ["-", "–", "—"])
    def test_dash_variants(self, dash):
        events = parse(f"10{dash}11 дело\n12 другое")
        assert (events[0].start.hour, events[0].end.hour) == (10, 11)


class TestFallbackToModel:
    """Не подошло под формат — пусть разбирает модель."""

    @pytest.mark.parametrize(
        "text",
        [
            "завтра в 15:00 созвон с командой",
            "купить молоко",
            "",
            "1800 сколково",                      # одна строка — не список
            "1800 сколково\nпросто текст",        # вторая строка без времени
            "1800 сколково\n25:00 невозможное",   # неверный час
            "1800 сколково\n10:70 невозможное",   # неверные минуты
            "1800\n1900",                         # время без названия
        ],
    )
    def test_returns_none(self, text):
        assert parse(text) is None

    def test_reversed_range_is_rejected(self):
        """«1800-1600» — скорее опечатка, чем событие через сутки."""
        assert parse("1800-1600 дело\n1900 другое") is None


class TestChaining:
    def test_first_event_uses_default_duration(self):
        events = parse("1800 сколково\n1930 физ шк")
        assert (events[0].start.hour, events[0].start.minute) == (17, 0)

    def test_default_duration_is_configurable(self):
        events = parse("1800 сколково\n1930 физ шк", minutes=15)
        assert (events[0].start.hour, events[0].start.minute) == (17, 45)

    def test_range_resets_the_chain(self):
        events = parse("10 первое\n1200-1300 второе\n14 третье")
        assert events[2].start == events[1].end

    def test_title_is_capitalised(self):
        assert parse("1800 сколково\n19 книга")[0].title == "Сколково"

    def test_inner_capitals_are_kept(self):
        assert parse("1800 физ шк ФМШ\n19 книга")[0].title == "Физ шк ФМШ"
