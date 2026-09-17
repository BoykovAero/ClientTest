"""Тесты разбора ссылок на Google Таблицы (сеть не нужна)."""

from __future__ import annotations

import pytest

from bot.sheets import SheetsReader, _rows_to_text, find_link, strip_link

REAL = (
    "https://docs.google.com/spreadsheets/d/"
    "1nLbvctk-cWN7H8c0LtHw0SKoYCuUrdUlByHCuNZC1qw/edit?gid=1585236605#gid=1585236605"
)


class TestFindLink:
    def test_real_message(self):
        """Ровно то, что прислал пользователь."""
        found = find_link(f"{REAL}\nдобавь отсюда расписание 11 е инж")
        assert found == ("1nLbvctk-cWN7H8c0LtHw0SKoYCuUrdUlByHCuNZC1qw", "1585236605")

    def test_link_without_gid(self):
        sheet_id, gid = find_link("https://docs.google.com/spreadsheets/d/" + "a" * 30 + "/edit")
        assert sheet_id == "a" * 30
        assert gid == ""

    @pytest.mark.parametrize(
        "text",
        [
            "просто текст",
            "",
            "https://docs.google.com/document/d/" + "a" * 30,  # это документ, не таблица
            "https://example.com/spreadsheets/d/" + "a" * 30,  # чужой домен
        ],
    )
    def test_not_a_sheet(self, text):
        assert find_link(text) is None


class TestStripLink:
    def test_leaves_only_the_request(self):
        """Хвост /edit?gid=...#gid=... не должен попадать в просьбу."""
        assert strip_link(f"{REAL}\nдобавь отсюда расписание 11 е инж") == (
            "добавь отсюда расписание 11 е инж"
        )

    def test_request_before_the_link(self):
        assert strip_link(f"возьми 11Е {REAL}") == "возьми 11Е"

    def test_link_alone_leaves_nothing(self):
        assert strip_link(REAL) == ""


class TestRowsToText:
    def test_cells_joined_like_xlsx(self):
        assert _rows_to_text([["Время", "11а"], ["9:00", "Алгебра"]]) == (
            "Время | 11а\n9:00 | Алгебра"
        )

    def test_empty_rows_and_cells_dropped(self):
        assert _rows_to_text([["дело", ""], [], ["  ", None]]) == "дело"

    def test_numbers_become_text(self):
        assert _rows_to_text([[1, 2.5]]) == "1 | 2.5"


class TestAvailability:
    def test_without_key_only_public_export(self):
        assert SheetsReader(None).available is False

    def test_with_key_api_is_possible(self):
        assert SheetsReader({"client_email": "a@b.com"}).available is True
