"""Разбор таблиц по координатам, а не по тексту.

Расписание — это сетка, и в ней важна позиция ячейки. Два свойства
настоящих таблиц ломают наивный разбор:

  - пустые ячейки. Если их выбросить, столбец «11Е инж» из шапки
    перестаёт совпадать со столбцом урока в строке ниже;
  - объединённые ячейки. Урок, общий для нескольких классов, записан
    одной ячейкой, и её значение лежит только в левом верхнем углу.

Поэтому сетка сначала достраивается целиком, и лишь потом из неё
берётся нужный столбец.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Сколько первых строк осматривать в поисках шапки.
HEADER_SEARCH_LINES = 8
# Сколько столбцов минимум должно быть в шапке, чтобы считать её шапкой.
MIN_HEADER_CELLS = 3
# Названия короче считаем слишком общими, чтобы искать их в просьбе.
MIN_NAME_LENGTH = 2


def normalise(value: str) -> str:
    """«11 е инж», «11Е ИНЖ» и «11еинж» — одно и то же."""
    return "".join(ch for ch in (value or "").lower() if ch.isalnum())


def fill_merges(
    rows: list[list[str]], merges: list[tuple[int, int, int, int]]
) -> list[list[str]]:
    """Растягивает значение объединённой области на все её ячейки.

    merges — четвёрки (первая строка, последняя строка, первый столбец,
    последний столбец), границы включительно.
    """
    grid = [list(row) for row in rows]

    def widen(row: int, width: int) -> None:
        if len(grid[row]) < width:
            grid[row].extend([""] * (width - len(grid[row])))

    for top, bottom, left, right in merges:
        if top >= len(grid) or left >= len(grid[top]):
            continue
        value = grid[top][left]
        if not value:
            continue
        for row in range(top, min(bottom + 1, len(grid))):
            widen(row, right + 1)
            for column in range(left, right + 1):
                grid[row][column] = value
    return grid


def find_header(rows: list[list[str]]) -> int:
    """Строка шапки — самая заполненная из первых нескольких."""
    best_index, best_count = -1, 0
    for index, row in enumerate(rows[:HEADER_SEARCH_LINES]):
        count = sum(1 for cell in row if cell.strip())
        if count > best_count:
            best_index, best_count = index, count
    return best_index if best_count >= MIN_HEADER_CELLS else -1


def find_column(rows: list[list[str]], instruction: str) -> tuple[int, str] | None:
    """Столбец, чьё название названо в просьбе. None — если неоднозначно."""
    wanted = normalise(instruction)
    if not wanted:
        return None

    header_index = find_header(rows)
    if header_index < 0:
        return None

    matches = []
    for column, cell in enumerate(rows[header_index]):
        name = normalise(cell)
        if column > 0 and len(name) >= MIN_NAME_LENGTH and name in wanted:
            matches.append((column, cell.strip()))

    if len(matches) != 1:
        if matches:
            logger.info(
                "Столбец не выбран: под просьбу подходит несколько — %s",
                ", ".join(name for _, name in matches),
            )
        return None
    return matches[0]


class ColumnChoiceNeeded(Exception):
    """В таблице несколько столбцов, и какой нужен — непонятно.

    Вываливать в модель всю таблицу в таком случае нельзя: она слепит
    события из заголовков и соседних колонок. Лучше спросить.
    """

    def __init__(self, choices: list[str], matched: bool = False) -> None:
        super().__init__("нужно выбрать столбец")
        self.choices = choices
        # True — варианты подобраны по просьбе, False — это просто все
        # столбцы листа. Первые куда полезнее показывать.
        self.matched = matched


def list_columns(rows: list[list[str]]) -> list[tuple[int, str]]:
    """Столбцы, которые можно предложить на выбор."""
    header_index = find_header(rows)
    if header_index < 0:
        return []

    seen: set[str] = set()
    columns = []
    for column, cell in enumerate(rows[header_index]):
        name = cell.strip()
        if column == 0 or len(normalise(name)) < MIN_NAME_LENGTH:
            continue
        # Объединённые заголовки повторяются в каждой своей колонке —
        # предлагать один и тот же класс дважды незачем.
        if normalise(name) in seen:
            continue
        seen.add(normalise(name))
        columns.append((column, name))
    return columns


def _tokens(instruction: str) -> list[str]:
    """Слова просьбы, по которым имеет смысл искать столбец.

    Если среди них есть слова с цифрами, берём только их: в расписании
    класс называется «11е», и это куда более точная примета, чем «расписание».
    """
    words = [normalise(word) for word in re.split(r"[^\w]+", instruction or "")]
    words = [word for word in words if len(word) >= MIN_NAME_LENGTH]

    numbered = [word for word in words if any(ch.isdigit() for ch in word)]
    if numbered:
        return numbered
    # Без цифр короткие слова только мешают: «на», «по», «и».
    return [word for word in words if len(word) >= 3]


def candidates(rows: list[list[str]], instruction: str) -> tuple[list[tuple[int, str]], bool]:
    """Подходящие столбцы и признак того, что они подобраны по просьбе.

    Без просьбы вернутся все столбцы с matched=False: выбирать придётся
    человеку. С просьбой, которой ничего не отвечает, вернётся пусто — на
    этом листе искать нечего.
    """
    columns = list_columns(rows)
    wanted = normalise(instruction)
    if not wanted or not columns:
        return columns, False

    # Сначала точное: название столбца целиком названо в просьбе.
    exact = [item for item in columns if normalise(item[1]) in wanted]
    if exact:
        return exact, True

    # Иначе по приметам: «расписание 11е» -> «11е инж», «11е УТ», «11е ИТ».
    tokens = _tokens(instruction)
    partial = [
        item
        for item in columns
        if any(token in normalise(item[1]) for token in tokens)
    ]
    return partial, bool(partial)


def narrow_or_ask(rows: list[list[str]], instruction: str) -> tuple[str, str]:
    """Сужает таблицу до нужного столбца или просит выбрать.

    Возвращает (текст, имя столбца). Если подходящих столбцов несколько,
    бросает ColumnChoiceNeeded со списком названий: разбирать всю таблицу
    в таком случае нельзя, модель слепит события из заголовков.
    """
    columns = list_columns(rows)
    if not columns:
        # Не таблица с колонками — отдаём как есть.
        return to_text(rows), ""

    found, matched = candidates(rows, instruction)
    if len(found) == 1:
        column, name = found[0]
        return narrow(rows, column), name
    if found:
        raise ColumnChoiceNeeded([name for _, name in found], matched=matched)
    if matched:
        return to_text(rows), ""
    raise ColumnChoiceNeeded([name for _, name in columns], matched=False)


def narrow(rows: list[list[str]], column: int, separator: str = " | ") -> str:
    """Оставляет первый столбец (время) и указанный, по строке на ряд."""
    lines = []
    for row in rows:
        left = row[0].strip() if row else ""
        right = row[column].strip() if column < len(row) else ""
        if left or right:
            lines.append(separator.join(part for part in (left, right) if part))
    return "\n".join(lines)


def to_text(rows: list[list[str]], separator: str = " | ") -> str:
    """Вся сетка текстом. Пустые ячейки отбрасываются — позиции уже не нужны."""
    lines = []
    for row in rows:
        cells = [cell.strip() for cell in row if cell and cell.strip()]
        if cells:
            lines.append(separator.join(cells))
    return "\n".join(lines)
