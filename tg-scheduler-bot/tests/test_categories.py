"""Тесты определения направления дела (сеть не нужна)."""

from __future__ import annotations

import pytest

from bot.categories import CATEGORIES, GROWTH, PERSONAL, STUDY, WORK, detect, normalise


@pytest.mark.parametrize(
    "text, expected",
    [
        # «школково» и «сколково» — разные вещи, и различаются по написанию.
        ("планировать школково по целям", STUDY),
        ("сколково доделать тгбот", WORK),
        ("бумажка сколк", WORK),
        ("1900-1930 сколково", WORK),
        # Школьные предметы — как они записаны на самом деле.
        ("Математика Флоринская Т.Р.", STUDY),
        ("физика Победимов А.К.", STUDY),
        ("2чфиз2зад", STUDY),
        ("1чпробфиз", STUDY),
        ("теорфиз", STUDY),
        ("урфиз", STUDY),
        ("Физкультура Акопян А.А.", STUDY),
        ("Информатика Владимирова В.И.", STUDY),
        ("додпробник по егэ", STUDY),
        # Олимпиады — это учёба, а не личное.
        ("олимпиада по физике", STUDY),
        ("1 тур олимпиады", STUDY),
        ("заключительный этап всош", STUDY),
        # Развитие.
        ("книга", GROWTH),
        ("аудиокнига+вер дум приглисть", GROWTH),
        ("клод настрой", GROWTH),
        ("установить клод", GROWTH),
        ("разобраться в заметках", GROWTH),
        # Личное.
        ("латинский язык", PERSONAL),
        ("встреча с Артёмом", PERSONAL),
        ("завтрак", PERSONAL),
        ("рутины+спать", PERSONAL),
    ],
)
def test_napravlenie_po_slovam(text, expected):
    assert detect(text) == expected


@pytest.mark.parametrize("text", ["", "написать утреннюю бумажку", "ыыы", "доделать"])
def test_neponyatnoe_ostayotsya_bez_napravleniya(text):
    """Лучше никуда, чем не туда: такое событие идёт в основной календарь."""
    assert detect(text) == ""


def test_bolshe_sovpadeniy_pereveshivaet():
    """«сколково доделать тгбот» — дважды Работа, спорить не о чем."""
    assert detect("сколково доделать тгбот") == WORK


def test_korotkie_slova_ne_lovyatsya_vnutri_drugih():
    """«мат» не должен находиться в «автомате», «тур» — в «турнире»."""
    assert detect("автомат") == ""
    assert detect("турнир по шахматам") == ""


def test_registr_i_yo_ne_meshayut():
    assert detect("КНИГА") == GROWTH
    assert detect("Учёба") == detect("Учеба")


class TestNormalise:
    """Ответ модели приводим к известному направлению."""

    @pytest.mark.parametrize("name", CATEGORIES)
    def test_izvestnye_prinimayutsya(self, name):
        assert normalise(name) == name

    def test_registr_i_probely_ne_meshayut(self):
        assert normalise("  работа  ") == WORK
        assert normalise("УЧЕБА") == STUDY

    @pytest.mark.parametrize("name", ["", "учёбка", "misc", None])
    def test_chuzhoe_otvergaetsya(self, name):
        assert normalise(name) == ""
