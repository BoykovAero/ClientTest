"""Модель данных дайджеста.

Один дайджест = один период (обычно месяц). Данные лежат в data/<period>.json,
код здесь только читает их и проверяет, что структура не поехала.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

CATEGORIES = {
    "models": "Модели",
    "agents": "Агенты",
    "regulation": "Регулирование",
    "infrastructure": "Инфраструктура",
    "hardware": "Железо",
    "adoption": "Внедрение",
    "industry": "Индустрия",
    "safety": "Безопасность",
    "research": "Исследования",
}

# Порядок разделов в готовом Markdown.
CATEGORY_ORDER = list(CATEGORIES)


class DigestError(ValueError):
    """Данные дайджеста не проходят проверку."""


@dataclass(frozen=True)
class DigestItem:
    """Одна новость: заголовок, суть, почему это важно, источники."""

    id: str
    category: str
    headline: str
    body: str
    why_it_matters: str
    sources: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict) -> "DigestItem":
        missing = {"id", "category", "headline", "body", "why_it_matters"} - raw.keys()
        if missing:
            raise DigestError(f"в новости не хватает полей: {sorted(missing)}")
        if raw["category"] not in CATEGORIES:
            raise DigestError(
                f"{raw['id']}: неизвестная категория {raw['category']!r}, "
                f"допустимые: {sorted(CATEGORIES)}"
            )
        sources = list(raw.get("sources", []))
        if not sources:
            raise DigestError(f"{raw['id']}: нужен хотя бы один источник")
        return cls(
            id=raw["id"],
            category=raw["category"],
            headline=raw["headline"],
            body=raw["body"],
            why_it_matters=raw["why_it_matters"],
            sources=sources,
        )

    @property
    def category_title(self) -> str:
        return CATEGORIES[self.category]


@dataclass(frozen=True)
class Digest:
    """Дайджест за период."""

    period: str
    title: str
    compiled_at: str
    summary: str
    items: list[DigestItem]
    outlook: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict) -> "Digest":
        missing = {"period", "title", "compiled_at", "summary", "items"} - raw.keys()
        if missing:
            raise DigestError(f"в дайджесте не хватает полей: {sorted(missing)}")

        items = [DigestItem.from_dict(item) for item in raw["items"]]
        if not items:
            raise DigestError("дайджест без новостей")

        ids = [item.id for item in items]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise DigestError(f"повторяющиеся id: {duplicates}")

        return cls(
            period=raw["period"],
            title=raw["title"],
            compiled_at=raw["compiled_at"],
            summary=raw["summary"],
            items=items,
            outlook=list(raw.get("outlook", [])),
        )

    @classmethod
    def load(cls, path: str | Path) -> "Digest":
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    def by_category(self) -> dict[str, list[DigestItem]]:
        """Новости, сгруппированные по категориям в порядке CATEGORY_ORDER."""
        grouped: dict[str, list[DigestItem]] = {}
        for category in CATEGORY_ORDER:
            found = [item for item in self.items if item.category == category]
            if found:
                grouped[category] = found
        return grouped
