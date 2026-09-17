"""Извлечение текста из присланных файлов.

Word и Excel разбираются локально, без обращения к моделям: так точнее и
бесплатно. Полученный текст дальше идёт через тот же разбор, что и обычное
сообщение.
"""

from __future__ import annotations

import csv
import io
import logging
from pathlib import PurePosixPath

logger = logging.getLogger(__name__)

# Telegram отдаёт ботам файлы не больше 20 МБ, но расписание столько не весит.
MAX_FILE_BYTES = 10 * 1024 * 1024
# Ограничение на объём текста, уходящего в модель: расписание на год может
# быть огромным, а разбирать всё разом и дорого, и бессмысленно.
MAX_TEXT_CHARS = 12_000

DOCUMENT_SUFFIXES = {".docx", ".xlsx", ".txt", ".md", ".csv"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic"}


class DocumentError(RuntimeError):
    """Файл не удалось прочитать. Текст уходит пользователю."""


def kind_of(filename: str, mime_type: str = "") -> str:
    """Что это за файл: 'document', 'image' или 'unsupported'."""
    suffix = PurePosixPath(filename or "").suffix.lower()
    if suffix in DOCUMENT_SUFFIXES:
        return "document"
    if suffix in IMAGE_SUFFIXES or (mime_type or "").startswith("image/"):
        return "image"
    return "unsupported"


def extract_text(filename: str, data: bytes) -> str:
    """Файл -> текст. Бросает DocumentError с внятной причиной."""
    if not data:
        raise DocumentError("файл пустой")
    if len(data) > MAX_FILE_BYTES:
        raise DocumentError(f"файл больше {MAX_FILE_BYTES // (1024 * 1024)} МБ")

    suffix = PurePosixPath(filename or "").suffix.lower()
    if suffix == ".docx":
        text = _from_docx(data)
    elif suffix == ".xlsx":
        text = _from_xlsx(data)
    elif suffix in {".txt", ".md"}:
        text = _decode(data)
    elif suffix == ".csv":
        text = _from_csv(data)
    else:
        raise DocumentError(
            f"формат {suffix or '?'} не поддерживается. "
            "Пришли .docx, .xlsx, .csv, .txt или картинку"
        )

    text = text.strip()
    if not text:
        raise DocumentError(
            "в файле не нашлось текста. Если это скан или картинка внутри "
            "документа — пришли её отдельно как изображение"
        )

    if len(text) > MAX_TEXT_CHARS:
        logger.info("Файл %r обрезан с %d до %d символов", filename, len(text), MAX_TEXT_CHARS)
        text = text[:MAX_TEXT_CHARS]
    return text


def _decode(data: bytes) -> str:
    """Текст в неизвестной кодировке. Windows-1251 всё ещё встречается."""
    for encoding in ("utf-8", "utf-8-sig", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise DocumentError("не удалось определить кодировку файла")


def _from_csv(data: bytes) -> str:
    rows = csv.reader(io.StringIO(_decode(data)))
    return "\n".join(" | ".join(cell.strip() for cell in row if cell.strip()) for row in rows)


def _from_docx(data: bytes) -> str:
    """Абзацы и таблицы. Расписания часто лежат именно в таблицах."""
    try:
        import docx
    except ImportError:
        raise DocumentError("на сервере не установлен python-docx") from None

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:
        raise DocumentError(f"файл не читается как .docx ({exc})") from exc

    lines = [p.text.strip() for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                lines.append(" | ".join(cells))
    return "\n".join(lines)


def _from_xlsx(data: bytes) -> str:
    """Все листы построчно. Пустые строки и столбцы отбрасываются."""
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise DocumentError("на сервере не установлен openpyxl") from None

    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise DocumentError(f"файл не читается как .xlsx ({exc})") from exc

    lines: list[str] = []
    try:
        for sheet in workbook.worksheets:
            if len(workbook.worksheets) > 1:
                lines.append(f"# лист: {sheet.title}")
            for row in sheet.iter_rows(values_only=True):
                cells = [str(value).strip() for value in row if value is not None and str(value).strip()]
                if cells:
                    lines.append(" | ".join(cells))
    finally:
        workbook.close()
    return "\n".join(lines)
