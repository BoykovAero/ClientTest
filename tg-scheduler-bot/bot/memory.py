"""Память бота: недавний разговор и заметки о человеке.

Хранится обычным JSON-файлом. База тут не нужна: пишет один человек,
записей сотни, а файл переживает перезапуск и читается глазами, когда
надо понять, что бот про тебя помнит.

Память не должна ронять бота. Нечитаемый или незаписываемый файл — повод
работать без неё и сказать об этом в лог, а не прекращать разговор.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Сколько ходов разговора хранить и сколько из них показывать модели.
# Больше восьми она уже не удерживает, а хранить полезно шире: из истории
# человек и сам видит, что бот делал.
TURN_LIMIT = 60
PROMPT_TURNS = 8
# Сколько заметок о себе помнить.
NOTE_LIMIT = 50
# Расписание целиком в памяти не нужно — от него хватает начала.
MAX_STORED_CHARS = 600


@dataclass
class Turn:
    """Один ход разговора: что сказал человек и что ответил бот."""

    at: str
    you: str
    bot: str


def _trim(text: str) -> str:
    value = " ".join((text or "").split())
    if len(value) <= MAX_STORED_CHARS:
        return value
    return value[:MAX_STORED_CHARS] + "…"


class Memory:
    """Разговор и заметки, привязанные к пользователю.

    path=None — память только на время работы процесса. Так удобно в
    тестах и так же бот ведёт себя, если писать на диск не вышло.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._writable = path is not None
        self._data: dict[str, dict] = {}
        self._load()

    # ─── чтение и запись файла ──────────────────────────────────────────
    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            # Битый файл лучше оставить на месте: вдруг пригодится глазами.
            logger.warning("Память не прочиталась (%s), начинаю с пустой", exc)
            return
        if isinstance(raw, dict):
            self._data = {str(key): value for key, value in raw.items() if isinstance(value, dict)}
            logger.info("Память: %d собеседник(ов) из %s", len(self._data), self._path)

    def _save(self) -> None:
        if self._path is None or not self._writable:
            return
        temp = self._path.with_suffix(".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            # Замена целиком: оборванная запись не оставит полуфайл.
            os.replace(temp, self._path)
        except OSError as exc:
            # Одного раза достаточно: иначе лог забьётся на каждом сообщении.
            self._writable = False
            logger.warning(
                "Память не пишется в %s (%s) — работаю без неё до перезапуска",
                self._path,
                exc,
            )

    # ─── разговор ───────────────────────────────────────────────────────
    def _slot(self, user_id: int) -> dict:
        return self._data.setdefault(str(user_id), {"turns": [], "notes": []})

    def remember(self, user_id: int, you: str, bot: str) -> None:
        """Записывает ход разговора."""
        turn = Turn(at=datetime.now().isoformat(timespec="seconds"), you=_trim(you), bot=_trim(bot))
        turns = self._slot(user_id).setdefault("turns", [])
        turns.append(asdict(turn))
        del turns[:-TURN_LIMIT]
        self._save()

    def recent(self, user_id: int, limit: int = PROMPT_TURNS) -> list[Turn]:
        turns = self._data.get(str(user_id), {}).get("turns", [])
        return [Turn(**turn) for turn in turns[-limit:] if isinstance(turn, dict)]

    # ─── заметки о человеке ─────────────────────────────────────────────
    def note(self, user_id: int, text: str) -> None:
        notes = self._slot(user_id).setdefault("notes", [])
        value = _trim(text)
        if value and value not in notes:
            notes.append(value)
            del notes[:-NOTE_LIMIT]
            self._save()

    def notes(self, user_id: int) -> list[str]:
        return list(self._data.get(str(user_id), {}).get("notes", []))

    # ─── всё вместе ─────────────────────────────────────────────────────
    def forget(self, user_id: int) -> None:
        self._data.pop(str(user_id), None)
        self._save()

    def as_prompt(self, user_id: int, limit: int = PROMPT_TURNS) -> str:
        """Память в виде куска подсказки. Пусто — значит помнить нечего."""
        lines: list[str] = []
        notes = self.notes(user_id)
        if notes:
            lines.append("Что помню про человека:")
            lines += [f"- {note}" for note in notes]
        turns = self.recent(user_id, limit)
        if turns:
            if lines:
                lines.append("")
            lines.append("Недавний разговор:")
            for turn in turns:
                lines.append(f"- человек: {turn.you}")
                lines.append(f"  бот: {turn.bot}")
        return "\n".join(lines)
