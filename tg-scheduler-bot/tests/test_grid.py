"""Тесты разбора таблиц по координатам."""

from __future__ import annotations

import pytest

from bot.grid import fill_merges, find_column, find_header, narrow, normalise, to_text


class TestNormalise:
    @pytest.mark.parametrize("value", ["11е инж", "11Е ИНЖ", "11 е инж", "11е\nинж"])
    def test_same_thing(self, value):
        assert normalise(value) == "11еинж"

    def test_empty(self):
        assert normalise("") == ""


class TestFillMerges:
    def test_value_spreads_across_the_area(self):
        rows = [["Физика", "", ""], ["", "", ""]]
        grid = fill_merges(rows, [(0, 1, 0, 2)])
        assert grid == [["Физика"] * 3, ["Физика"] * 3]

    def test_short_rows_are_widened(self):
        """В таблицах хвостовые пустые ячейки часто просто отсутствуют."""
        rows = [["Физика"], []]
        grid = fill_merges(rows, [(0, 1, 0, 2)])
        assert grid[1] == ["Физика"] * 3

    def test_empty_merge_changes_nothing(self):
        rows = [["", "А"], ["", "Б"]]
        assert fill_merges(rows, [(0, 1, 0, 0)]) == rows

    def test_source_is_not_modified(self):
        rows = [["Физика", ""]]
        fill_merges(rows, [(0, 0, 0, 1)])
        assert rows == [["Физика", ""]]

    def test_merge_beyond_the_grid_is_ignored(self):
        rows = [["А"]]
        assert fill_merges(rows, [(5, 6, 0, 1)]) == [["А"]]


class TestFindHeader:
    def test_picks_the_fullest_row(self):
        rows = [[""], ["Время", "11а", "11б", "11в"], ["9:00", "", "", ""]]
        assert find_header(rows) == 1

    def test_no_header_in_sparse_table(self):
        assert find_header([["а"], ["б"]]) == -1


class TestFindColumn:
    HEADER = ["Время", "11а", "кабинет", "11е инж", "кабинет", "11е УТ"]
    ROWS = [HEADER, ["9:00-9:40", "", "", "Физика", "4.22", ""]]

    def test_finds_the_named_column(self):
        assert find_column(self.ROWS, "добавь расписание 11 е инж") == (3, "11е инж")

    def test_position_survives_empty_cells(self):
        """Главное свойство: номер столбца из шапки годится для строк ниже."""
        column, _ = find_column(self.ROWS, "11е инж")
        assert self.ROWS[1][column] == "Физика"

    def test_ambiguous_request(self):
        assert find_column(self.ROWS, "возьми 11е инж и 11е УТ") is None

    def test_nothing_matches(self):
        assert find_column(self.ROWS, "расписание 9в") is None

    def test_empty_instruction(self):
        assert find_column(self.ROWS, "") is None

    def test_first_column_is_never_chosen(self):
        """Первый столбец — время, он берётся всегда и не выбирается по имени."""
        assert find_column(self.ROWS, "возьми время") is None


class TestNarrow:
    ROWS = [
        ["Время", "11а", "кабинет", "11е инж"],
        ["понедельник", "", "", "11е инж"],
        ["9:00-9:40", "Алгебра", "2.1", "Физика"],
        ["", "", "", ""],
    ]

    def test_keeps_time_and_column(self):
        assert narrow(self.ROWS, 3).splitlines()[2] == "9:00-9:40 | Физика"

    def test_day_names_survive(self):
        assert "понедельник" in narrow(self.ROWS, 3)

    def test_blank_rows_are_dropped(self):
        assert len(narrow(self.ROWS, 3).splitlines()) == 3

    def test_missing_cell_is_not_an_error(self):
        assert narrow([["9:00"]], 3) == "9:00"


class TestToText:
    def test_empty_cells_dropped(self):
        assert to_text([["а", "", "б"], ["", ""]]) == "а | б"
