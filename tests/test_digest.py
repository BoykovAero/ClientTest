"""Тесты модели и рендеринга дайджеста."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_digest.models import Digest, DigestError, DigestItem
from ai_digest.render import render_markdown

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _item(**overrides) -> dict:
    base = {
        "id": "test-item",
        "category": "models",
        "headline": "Заголовок",
        "body": "Текст новости.",
        "why_it_matters": "Потому что.",
        "sources": ["https://example.com"],
    }
    base.update(overrides)
    return base


def _digest(**overrides) -> dict:
    base = {
        "period": "2026-08",
        "title": "Тестовый дайджест",
        "compiled_at": "2026-09-02",
        "summary": "Кратко.",
        "items": [_item()],
        "outlook": ["Что-то будет."],
    }
    base.update(overrides)
    return base


def test_parses_valid_digest():
    digest = Digest.from_dict(_digest())
    assert digest.period == "2026-08"
    assert len(digest.items) == 1
    assert digest.items[0].category_title == "Модели"


def test_rejects_unknown_category():
    with pytest.raises(DigestError, match="неизвестная категория"):
        DigestItem.from_dict(_item(category="погода"))


def test_rejects_item_without_sources():
    with pytest.raises(DigestError, match="источник"):
        DigestItem.from_dict(_item(sources=[]))


def test_rejects_missing_field():
    raw = _item()
    del raw["body"]
    with pytest.raises(DigestError, match="не хватает полей"):
        DigestItem.from_dict(raw)


def test_rejects_duplicate_ids():
    raw = _digest(items=[_item(), _item()])
    with pytest.raises(DigestError, match="повторяющиеся id"):
        Digest.from_dict(raw)


def test_rejects_empty_digest():
    with pytest.raises(DigestError, match="без новостей"):
        Digest.from_dict(_digest(items=[]))


def test_by_category_follows_declared_order():
    raw = _digest(
        items=[
            _item(id="b", category="safety"),
            _item(id="a", category="models"),
        ]
    )
    assert list(Digest.from_dict(raw).by_category()) == ["models", "safety"]


def test_markdown_contains_every_item_and_source():
    digest = Digest.from_dict(_digest())
    md = render_markdown(digest)
    assert "# Тестовый дайджест" in md
    assert "### Заголовок" in md
    assert "<https://example.com>" in md
    assert "Что дальше" in md


@pytest.mark.parametrize("path", sorted(DATA_DIR.glob("*.json")), ids=lambda p: p.stem)
def test_shipped_data_is_valid(path: Path):
    """Реальные данные в data/ должны проходить те же проверки."""
    digest = Digest.load(path)
    assert digest.period == path.stem
    assert digest.items
    render_markdown(digest)


@pytest.mark.parametrize("path", sorted(DATA_DIR.glob("*.json")), ids=lambda p: p.stem)
def test_shipped_sources_are_https(path: Path):
    raw = json.loads(path.read_text(encoding="utf-8"))
    for item in raw["items"]:
        for url in item["sources"]:
            assert url.startswith("https://"), f"{item['id']}: {url}"
