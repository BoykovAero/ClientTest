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
