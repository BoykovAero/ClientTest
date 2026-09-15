"""Тесты валидации ответа модели (сеть не нужна)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bot.parser import ParseError, build_user_prompt, events_from_payload

MSK = ZoneInfo("Europe/Moscow")
DEFAULT_MINUTES = 60


def parse(payload: dict, minutes: int = DEFAULT_MINUTES):
    return events_from_payload(payload, MSK, minutes)


class TestPayloadShape:
    def test_missing_events_key_raises(self):
        with pytest.raises(ParseError, match="events"):
            parse({"nonsense": 1})

    def test_empty_list_gives_no_events(self):
        assert parse({"events": []}) == []

    def test_non_object_items_are_skipped(self):
        events = parse({"events": ["строка", 42, None]})
        assert events == []


class TestSingleEvent:
    def test_parses_timed_event(self):
        events = parse(
            {
                "events": [
                    {
                        "title": "Созвон",
                        "start": "2026-09-16T15:00:00",
                        "end": "2026-09-16T16:00:00",
                        "all_day": False,
                        "notes": "с командой",
                    }
                ]
            }
        )
        assert len(events) == 1
        event = events[0]
        assert event.title == "Созвон"
        assert event.start == datetime(2026, 9, 16, 15, 0, tzinfo=MSK)
        assert event.end == datetime(2026, 9, 16, 16, 0, tzinfo=MSK)
        assert event.notes == "с командой"
        assert not event.all_day

    def test_naive_time_gets_configured_timezone(self):
        event = parse({"events": [{"title": "Зал", "start": "2026-09-16T18:30:00"}]})[0]
        assert event.start.tzinfo is MSK

    def test_offset_in_response_is_converted(self):
        """Модель вернула время с зоной — приводим к нашей, а не игнорируем."""
        event = parse({"events": [{"title": "Звонок", "start": "2026-09-16T12:00:00+00:00"}]})[0]
        assert event.start == datetime(2026, 9, 16, 15, 0, tzinfo=MSK)

    def test_trailing_z_is_understood(self):
        event = parse({"events": [{"title": "Звонок", "start": "2026-09-16T12:00:00Z"}]})[0]
        assert event.start == datetime(2026, 9, 16, 15, 0, tzinfo=MSK)


class TestEndTimeFallback:
    def test_missing_end_uses_default_duration(self):
        event = parse({"events": [{"title": "Зал", "start": "2026-09-16T18:30:00"}]})[0]
        assert event.end == datetime(2026, 9, 16, 19, 30, tzinfo=MSK)

    def test_default_duration_is_configurable(self):
        event = parse(
            {"events": [{"title": "Кофе", "start": "2026-09-16T10:00:00"}]}, minutes=15
        )[0]
        assert event.end == datetime(2026, 9, 16, 10, 15, tzinfo=MSK)

    def test_end_before_start_is_repaired(self):
        event = parse(
            {
                "events": [
                    {
                        "title": "Встреча",
                        "start": "2026-09-16T15:00:00",
                        "end": "2026-09-16T14:00:00",
                    }
                ]
            }
        )[0]
        assert event.end == datetime(2026, 9, 16, 16, 0, tzinfo=MSK)

    def test_zero_length_event_is_repaired(self):
        event = parse(
            {
                "events": [
                    {
                        "title": "Встреча",
                        "start": "2026-09-16T15:00:00",
                        "end": "2026-09-16T15:00:00",
                    }
                ]
            }
        )[0]
        assert event.end == datetime(2026, 9, 16, 16, 0, tzinfo=MSK)

    def test_unparsable_end_falls_back_to_default(self):
        event = parse(
            {"events": [{"title": "Встреча", "start": "2026-09-16T15:00:00", "end": "позже"}]}
        )[0]
        assert event.end == datetime(2026, 9, 16, 16, 0, tzinfo=MSK)


class TestAllDay:
    def test_date_only_values(self):
        event = parse(
            {
                "events": [
                    {
                        "title": "Отпуск",
                        "start": "2026-09-16",
                        "end": "2026-09-18",
                        "all_day": True,
                    }
                ]
            }
        )[0]
        assert event.all_day
        assert event.start_date().isoformat() == "2026-09-16"
        assert event.end_date_exclusive().isoformat() == "2026-09-19"

    def test_all_day_without_end_stays_one_day(self):
        event = parse({"events": [{"title": "Дедлайн", "start": "2026-09-16", "all_day": True}]})[0]
        assert event.end_date_exclusive().isoformat() == "2026-09-17"


class TestBadItemsSkipped:
    def test_event_without_title_is_skipped(self):
        assert parse({"events": [{"start": "2026-09-16T15:00:00"}]}) == []

    def test_event_with_unparsable_start_is_skipped(self):
        assert parse({"events": [{"title": "Что-то", "start": "когда-нибудь"}]}) == []

    def test_one_bad_item_does_not_drop_the_good_ones(self):
        events = parse(
            {
                "events": [
                    {"title": "Плохое", "start": "не дата"},
                    {"title": "Хорошее", "start": "2026-09-16T15:00:00"},
                ]
            }
        )
        assert [event.title for event in events] == ["Хорошее"]


class TestUserPrompt:
    def test_includes_now_weekday_and_timezone(self):
        prompt = build_user_prompt(
            "завтра в 15:00 созвон", datetime(2026, 9, 16, 8, 45, tzinfo=MSK), "Europe/Moscow"
        )
        assert "2026-09-16" in prompt
        assert "среда" in prompt
        assert "Europe/Moscow" in prompt
        assert "завтра в 15:00 созвон" in prompt

    def test_states_tomorrows_date_explicitly(self):
        prompt = build_user_prompt("", datetime(2026, 9, 16, 8, 45, tzinfo=MSK), "Europe/Moscow")
        assert "2026-09-17" in prompt
