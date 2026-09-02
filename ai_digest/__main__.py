"""CLI: собрать Markdown-дайджест из JSON-данных.

    python -m ai_digest                 # все периоды из data/ в docs/
    python -m ai_digest --period 2026-08
    python -m ai_digest --check         # только проверить данные
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ai_digest.models import Digest, DigestError
from ai_digest.render import render_markdown

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"


def _data_files(period: str | None) -> list[Path]:
    if period:
        path = DATA_DIR / f"{period}.json"
        if not path.exists():
            raise SystemExit(f"нет данных за период {period}: {path}")
        return [path]
    files = sorted(DATA_DIR.glob("*.json"))
    if not files:
        raise SystemExit(f"в {DATA_DIR} нет JSON-файлов")
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ai_digest", description=__doc__)
    parser.add_argument("--period", help="период вида 2026-08; по умолчанию все")
    parser.add_argument(
        "--check",
        action="store_true",
        help="только проверить данные, ничего не записывать",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DOCS_DIR,
        help=f"куда писать Markdown (по умолчанию {DOCS_DIR})",
    )
    args = parser.parse_args(argv)

    failed = False
    for path in _data_files(args.period):
        try:
            digest = Digest.load(path)
        except DigestError as exc:
            print(f"{path.name}: ОШИБКА — {exc}", file=sys.stderr)
            failed = True
            continue

        if args.check:
            print(f"{path.name}: ок, новостей — {len(digest.items)}")
            continue

        args.out_dir.mkdir(parents=True, exist_ok=True)
        out_path = args.out_dir / f"digest-{digest.period}.md"
        out_path.write_text(render_markdown(digest), encoding="utf-8")
        print(f"{path.name} -> {out_path.relative_to(ROOT)}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
