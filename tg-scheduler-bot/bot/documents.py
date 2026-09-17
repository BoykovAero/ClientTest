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
# Предел на объём текста. Он высокий, потому что разбор идёт частями:
# в модель за раз уходит кусок, а не весь файл. Ограничение остаётся ради
# страховки от по-настоящему гигантских выгрузок.
MAX_TEXT_CHARS = 60_000

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


def is_large(text: str) -> bool:
    """Стоит ли предупредить пользователя, что разбор займёт время."""
    from bot.parser import CHUNK_CHARS

    return len(text) > CHUNK_CHARS


# Доля управляющих символов, выше которой считаем, что перед нами не текст.
MAX_CONTROL_RATIO = 0.05


def _looks_like_text(text: str) -> bool:
    """Отсекает двоичные файлы, притворившиеся текстом.

    cp1251 однобайтовая и «расшифровывает» практически любой мусор, поэтому
    успешного decode мало: без этой проверки картинка, названная .txt, ушла
    бы в модель кракозябрами.
    """
    if not text:
        return False
    control = sum(1 for ch in text if ch < " " and ch not in "\n\r\t")
    return control / len(text) <= MAX_CONTROL_RATIO


def _decode(data: bytes) -> str:
    """Текст в неизвестной кодировке. Windows-1251 всё ещё встречается."""
    for encoding in ("utf-8", "utf-8-sig", "cp1251"):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        if _looks_like_text(text):
            return text
    raise DocumentError("файл не похож на текст — проверь формат и кодировку")


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
