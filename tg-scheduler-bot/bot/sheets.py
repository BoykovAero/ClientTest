"""Чтение расписаний по ссылке на Google Таблицу.

Сначала пробуем прочитать через API от имени сервисного аккаунта — так
открываются и закрытые таблицы, если им выдан доступ. Если доступа нет,
пробуем выгрузку в CSV, которая работает у таблиц, открытых по ссылке.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import urllib.error
import urllib.request

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from bot.grid import fill_merges, find_column, narrow, to_text

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
TIMEOUT = 30

# \S* в конце съедает хвост вида /edit?gid=...#gid=... — иначе он
# остаётся в тексте и попадает в просьбу пользователя.
LINK = re.compile(r"https://docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]{20,})\S*")
GID = re.compile(r"[#?&]gid=(\d+)")
# Ссылку часто присылают обрезанной — с многоточием вместо идентификатора.
# Такую надо отличать от «в сообщении вообще нет таблицы».
MENTION = re.compile(r"docs\.google\.com/spreadsheets")


class SheetsError(RuntimeError):
    """Таблицу не удалось прочитать. Текст уходит пользователю."""


def find_link(text: str) -> tuple[str, str] | None:
    """Ищет в тексте ссылку на таблицу. Возвращает (id, gid) или None."""
    match = LINK.search(text or "")
    if not match:
        return None
    gid_match = GID.search(text)
    return match.group(1), (gid_match.group(1) if gid_match else "")


def looks_like_sheet(text: str) -> bool:
    """Упоминается таблица, но ссылку разобрать не удалось."""
    return bool(MENTION.search(text or ""))


def strip_link(text: str) -> str:
    """Убирает ссылку из текста — остаётся просьба пользователя."""
    return LINK.sub("", text or "").strip()


def _rows_to_text(rows: list[list]) -> str:
    """Сетка в текст. Пустые ячейки отбрасываются — позиции уже не нужны."""
    return to_text([[_clean(cell) for cell in row] for row in rows])


def _clean(cell) -> str:
    """Пустую ячейку API отдаёт как None, и str() превратил бы её в «None»."""
    return "" if cell is None else " ".join(str(cell).split())


class SheetsReader:
    def __init__(self, service_account_info: dict | None) -> None:
        self._info = service_account_info
        self._service = None

    @property
    def available(self) -> bool:
        """Есть ли чем читать закрытые таблицы."""
        return self._info is not None

    def _get_service(self):
        if self._service is None:
            credentials = service_account.Credentials.from_service_account_info(
                self._info, scopes=SCOPES
            )
            self._service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
        return self._service

    def read(self, sheet_id: str, gid: str = "", instruction: str = "") -> tuple[str, str]:
        """Таблица -> (текст, имя выбранного столбца).

        Если в просьбе назван столбец, в текст попадает только он и столбец
        времени. Бросает SheetsError с внятной причиной.
        """
        if self.available:
            try:
                return self._read_api(sheet_id, gid, instruction)
            except SheetsError as exc:
                logger.info("Таблица через API не открылась (%s), пробую выгрузку", exc)
        return self._read_public(sheet_id, gid, instruction)

    # ─── через API, от имени сервисного аккаунта ────────────────────────────
    def _read_api(self, sheet_id: str, gid: str, instruction: str) -> tuple[str, str]:
        """Читает лист вместе с объединёнными областями.

        includeGridData обязателен: без него общий для нескольких классов
        урок виден только в колонке первого из них.
        """
        try:
            service = self._get_service()
            meta = (
                service.spreadsheets()
                .get(
                    spreadsheetId=sheet_id,
                    includeGridData=True,
                    fields=(
                        "sheets(properties(sheetId,title),merges,"
                        "data(rowData(values(formattedValue))))"
                    ),
                )
                .execute()
            )
        except HttpError as exc:
            raise SheetsError(_describe(exc)) from exc
        except Exception as exc:
            raise SheetsError(str(exc)) from exc

        sheets = meta.get("sheets", [])
        if not sheets:
            raise SheetsError("в таблице нет ни одного листа")

        sheet = sheets[0]
        if gid:
            for candidate in sheets:
                if str(candidate["properties"].get("sheetId")) == gid:
                    sheet = candidate
                    break
        title = sheet["properties"]["title"]

        rows = [
            [_clean(cell.get("formattedValue")) for cell in row.get("values", [])]
            for block in sheet.get("data", [])
            for row in block.get("rowData", [])
        ]
        if not rows:
            raise SheetsError(f"лист {title!r} пуст")

        merges = [
            (
                m.get("startRowIndex", 0),
                m.get("endRowIndex", 0) - 1,
                m.get("startColumnIndex", 0),
                m.get("endColumnIndex", 0) - 1,
            )
            for m in sheet.get("merges", [])
        ]
        grid = fill_merges(rows, merges)

        found = find_column(grid, instruction) if instruction else None
        if found is not None:
            column, name = found
            logger.info("Таблица: лист %r, столбец %r", title, name)
            return narrow(grid, column), name

        logger.info("Таблица: лист %r, %d строк целиком", title, len(rows))
        return to_text(grid), ""

    # ─── выгрузка в CSV, для таблиц, открытых по ссылке ─────────────────────
    def _read_public(self, sheet_id: str, gid: str, instruction: str = "") -> tuple[str, str]:
        url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
        if gid:
            url += f"&gid={gid}"
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 404):
                raise SheetsError(
                    "нет доступа к таблице. Открой её настройками доступа боту: "
                    "«Настройки доступа» -> добавь адрес сервисного аккаунта с правом "
                    "чтения. Либо включи доступ по ссылке"
                ) from exc
            raise SheetsError(f"Google ответил {exc.code}") from exc
        except Exception as exc:
            raise SheetsError(f"не смог скачать таблицу: {exc}") from exc

        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError:
            decoded = raw.decode("cp1251", errors="replace")

        # У выгрузки нет сведений об объединениях, но позиции ячеек в ней
        # сохранены — этого хватает, чтобы выбрать нужный столбец.
        rows = [[_clean(cell) for cell in row] for row in csv.reader(io.StringIO(decoded))]
        found = find_column(rows, instruction) if instruction else None
        if found is not None:
            column, name = found
            logger.info("Таблица из выгрузки: столбец %r", name)
            return narrow(rows, column), name

        text = to_text(rows)
        if not text:
            raise SheetsError("таблица пуста")
        logger.info("Таблица прочитана выгрузкой в CSV")
        return text, ""


def _describe(exc: HttpError) -> str:
    status = getattr(getattr(exc, "resp", None), "status", 0)
    if status in (403, 404):
        return "нет доступа к таблице"
    if status == 400:
        return "неверный запрос к таблице"
    return f"Google ответил {status}"
