"""Тесты просмотра и удаления событий (сеть не нужна)."""

from __future__ import annotations

import pytest

from bot.calendars.base import CalendarEntry, CalendarError
from bot.telegram_bot import DELETE_LIMIT, SchedulerBot


class FakeGoogle:
    def __init__(self, fail=False):
        self.deleted: list[str] = []
        self._fail = fail

    def delete(self, event_id: str) -> None:
        if self._fail:
            raise CalendarError("404: календарь не найден")
        self.deleted.append(event_id)


class FakeICloud:
    def __init__(self, present=True, fail=False):
        self.deleted: list[str] = []
        self._present = present
        self._fail = fail

    def delete_by_uid(self, uid: str) -> bool:
        if self._fail:
            raise CalendarError("401: неверный пароль")
        self.deleted.append(uid)
        return self._present


def make_bot(google=None, icloud=None) -> SchedulerBot:
    return SchedulerBot(
        config=object(),
        parser=object(),
        transcriber=object(),
        image_reader=object(),
        openai_client=object(),
        google=google or FakeGoogle(),
        icloud=icloud or FakeICloud(),
    )


ENTRY = CalendarEntry(event_id="ev1", when="15:00", title="Созвон", uid="abc123")
FOREIGN = CalendarEntry(event_id="ev2", when="18:00", title="Чужое", uid="")


class TestEntryLookup:
    def test_finds_by_number(self):
        bot = make_bot()
        bot._listings["tok"] = [ENTRY, FOREIGN]
        assert bot._entry("tok", "2") is FOREIGN

    def test_unknown_token_gives_nothing(self):
        assert make_bot()._entry("нет-такого", "1") is None

    @pytest.mark.parametrize("number", ["0", "3", "-1", "абв", ""])
    def test_bad_numbers_are_rejected(self, number):
        bot = make_bot()
        bot._listings["tok"] = [ENTRY, FOREIGN]
        assert bot._entry("tok", number) is None


class TestRemoval:
    @pytest.mark.asyncio
    async def test_deletes_from_both_calendars(self):
        google, icloud = FakeGoogle(), FakeICloud()
        results = await make_bot(google, icloud)._remove(ENTRY)
        assert google.deleted == ["ev1"]
        assert icloud.deleted == ["abc123"]
        assert results == ["Google ✓", "iCloud ✓"]

    @pytest.mark.asyncio
    async def test_foreign_event_skips_icloud(self):
        """У чужого события нет UID — искать в iCloud нечего."""
        google, icloud = FakeGoogle(), FakeICloud()
        results = await make_bot(google, icloud)._remove(FOREIGN)
        assert google.deleted == ["ev2"]
        assert icloud.deleted == []
        assert "пары нет" in results[1]

    @pytest.mark.asyncio
    async def test_missing_in_icloud_is_not_an_error(self):
        results = await make_bot(FakeGoogle(), FakeICloud(present=False))._remove(ENTRY)
        assert "уже не было" in results[1]

    @pytest.mark.asyncio
    async def test_google_failure_does_not_stop_icloud(self):
        """Частичный успех должен быть виден, а не проглочен."""
        google, icloud = FakeGoogle(fail=True), FakeICloud()
        results = await make_bot(google, icloud)._remove(ENTRY)
        assert results[0].startswith("Google ✗")
        assert results[1] == "iCloud ✓"
        assert icloud.deleted == ["abc123"]

    @pytest.mark.asyncio
    async def test_icloud_failure_is_reported(self):
        results = await make_bot(FakeGoogle(), FakeICloud(fail=True))._remove(ENTRY)
        assert results[0] == "Google ✓"
        assert results[1].startswith("iCloud ✗")


class TestKeyboard:
    def test_buttons_are_numbered_from_one(self):
        markup = SchedulerBot._number_keyboard("tok", 3)
        labels = [b.text for row in markup.inline_keyboard for b in row]
        assert labels == ["1", "2", "3"]

    def test_rows_hold_five_buttons(self):
        markup = SchedulerBot._number_keyboard("tok", 12)
        assert [len(row) for row in markup.inline_keyboard] == [5, 5, 2]

    def test_callback_carries_token_and_number(self):
        markup = SchedulerBot._number_keyboard("tok", 1)
        assert markup.inline_keyboard[0][0].callback_data == "pick:tok:1"

    def test_callback_data_fits_telegram_limit(self):
        """Telegram отводит под callback_data 64 байта."""
        markup = SchedulerBot._number_keyboard("a" * 8, DELETE_LIMIT)
        for row in markup.inline_keyboard:
            for button in row:
                assert len(button.callback_data.encode("utf-8")) <= 64
