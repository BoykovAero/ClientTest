"""Тесты извлечения текста из файлов (сеть не нужна)."""

from __future__ import annotations

import io

import pytest

from bot.documents import MAX_FILE_BYTES, DocumentError, extract_text, kind_of


class TestKindOf:
    @pytest.mark.parametrize("name", ["план.docx", "Расписание.XLSX", "dates.csv", "a.txt"])
    def test_documents(self, name):
        assert kind_of(name) == "document"

    @pytest.mark.parametrize("name", ["снимок.png", "photo.JPG", "скан.webp"])
    def test_images_by_suffix(self, name):
        assert kind_of(name) == "image"

    def test_image_by_mime_when_suffix_is_odd(self):
        assert kind_of("IMG_0042", "image/heic") == "image"

    @pytest.mark.parametrize("name", ["архив.zip", "презентация.pptx", ""])
    def test_unsupported(self, name):
        assert kind_of(name) == "unsupported"


class TestGuards:
    def test_empty_file(self):
        with pytest.raises(DocumentError, match="пустой"):
            extract_text("a.txt", b"")

    def test_too_big(self):
        with pytest.raises(DocumentError, match="больше"):
            extract_text("a.txt", b"x" * (MAX_FILE_BYTES + 1))

    def test_unsupported_format_names_alternatives(self):
        with pytest.raises(DocumentError, match=r"\.docx"):
            extract_text("архив.zip", b"PK\x03\x04data")

    def test_whitespace_only_is_rejected(self):
        with pytest.raises(DocumentError, match="не нашлось текста"):
            extract_text("a.txt", b"   \n\n  ")


class TestPlainText:
    def test_utf8(self):
        text = extract_text("plan.txt", "завтра в 15:00 созвон".encode("utf-8"))
        assert "созвон" in text

    def test_cp1251(self):
        """Windows-1251 всё ещё встречается в выгрузках."""
        text = extract_text("plan.txt", "совещание в 10:00".encode("cp1251"))
        assert "совещание" in text

    def test_binary_disguised_as_text_is_rejected(self):
        """cp1251 «расшифрует» любой мусор — ловим по управляющим символам."""
        png_header = b"\x89PNG\r\n\x1a\n" + bytes(range(32)) * 20
        with pytest.raises(DocumentError, match="не похож на текст"):
            extract_text("plan.txt", png_header)

    def test_text_with_a_few_control_chars_still_passes(self):
        data = "созвон\x00 в 15:00\n" .encode("utf-8") + b"x" * 200
        assert "созвон" in extract_text("plan.txt", data)

    def test_csv_becomes_readable_rows(self):
        data = "дата,дело\n17.09,созвон\n18.09,спортзал\n".encode("utf-8")
        text = extract_text("plan.csv", data)
        assert "17.09 | созвон" in text
        assert "18.09 | спортзал" in text


class TestDocx:
    @staticmethod
    def build(paragraphs=(), table_rows=()):
        docx = pytest.importorskip("docx")
        document = docx.Document()
        for line in paragraphs:
            document.add_paragraph(line)
        if table_rows:
            table = document.add_table(rows=0, cols=len(table_rows[0]))
            for row in table_rows:
                cells = table.add_row().cells
                for cell, value in zip(cells, row):
                    cell.text = value
        buffer = io.BytesIO()
        document.save(buffer)
        return buffer.getvalue()

    def test_paragraphs(self):
        text = extract_text("plan.docx", self.build(paragraphs=["Понедельник: созвон в 15:00"]))
        assert "созвон в 15:00" in text

    def test_tables_are_included(self):
        """Расписания чаще всего лежат именно в таблицах."""
        data = self.build(table_rows=[["Дата", "Событие"], ["17.09", "Олимпиада"]])
        text = extract_text("plan.docx", data)
        assert "17.09 | Олимпиада" in text

    def test_empty_paragraphs_are_dropped(self):
        text = extract_text("plan.docx", self.build(paragraphs=["", "дело", "  "]))
        assert text.strip() == "дело"

    def test_broken_file(self):
        with pytest.raises(DocumentError, match="не читается"):
            extract_text("plan.docx", "не docx вовсе".encode("utf-8"))


class TestXlsx:
    @staticmethod
    def build(sheets):
        openpyxl = pytest.importorskip("openpyxl")
        workbook = openpyxl.Workbook()
        workbook.remove(workbook.active)
        for title, rows in sheets.items():
            sheet = workbook.create_sheet(title)
            for row in rows:
                sheet.append(row)
        buffer = io.BytesIO()
        workbook.save(buffer)
        return buffer.getvalue()

    def test_single_sheet(self):
        data = self.build({"Лист1": [["Дата", "Событие"], ["17.09", "Созвон"]]})
        text = extract_text("plan.xlsx", data)
        assert "17.09 | Созвон" in text

    def test_sheet_names_shown_when_several(self):
        data = self.build({"Сентябрь": [["17.09", "А"]], "Октябрь": [["01.10", "Б"]]})
        text = extract_text("plan.xlsx", data)
        assert "# лист: Сентябрь" in text
        assert "# лист: Октябрь" in text

    def test_empty_rows_and_cells_are_dropped(self):
        data = self.build({"Лист1": [["дело", None], [None, None], ["", "  "]]})
        text = extract_text("plan.xlsx", data)
        assert text.strip() == "дело"

    def test_broken_file(self):
        with pytest.raises(DocumentError, match="не читается"):
            extract_text("plan.xlsx", "не xlsx вовсе".encode("utf-8"))


class TestTruncation:
    def test_long_text_is_cut(self):
        from bot.documents import MAX_TEXT_CHARS

        data = ("строка расписания\n" * 5000).encode("utf-8")
        text = extract_text("plan.txt", data)
        assert len(text) == MAX_TEXT_CHARS
