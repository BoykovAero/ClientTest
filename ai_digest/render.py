"""Рендеринг дайджеста в Markdown."""

from __future__ import annotations

from ai_digest.models import CATEGORIES, Digest


def render_markdown(digest: Digest) -> str:
    """Собирает готовый Markdown-документ из дайджеста."""
    lines: list[str] = [
        f"# {digest.title}",
        "",
        f"*Период: {digest.period} · собрано {digest.compiled_at}*",
        "",
        "## Коротко",
        "",
        digest.summary,
        "",
    ]

    grouped = digest.by_category()

    lines += ["## Содержание", ""]
    for category, items in grouped.items():
        lines.append(f"- **{CATEGORIES[category]}**")
        for item in items:
            lines.append(f"  - [{item.headline}](#{item.id})")
    lines.append("")

    for category, items in grouped.items():
        lines += [f"## {CATEGORIES[category]}", ""]
        for item in items:
            lines += [
                f'<a id="{item.id}"></a>',
                "",
                f"### {item.headline}",
                "",
                item.body,
                "",
                f"**Почему это важно.** {item.why_it_matters}",
                "",
                "Источники:",
                "",
            ]
            lines += [f"- <{url}>" for url in item.sources]
            lines.append("")

    if digest.outlook:
        lines += ["## Что дальше", ""]
        lines += [f"- {point}" for point in digest.outlook]
        lines.append("")

    lines += [
        "---",
        "",
        "Документ собран автоматически из `data/"
        f"{digest.period}.json` командой `python -m ai_digest`.",
        "Правьте JSON, а не этот файл.",
        "",
    ]

    return "\n".join(lines)
