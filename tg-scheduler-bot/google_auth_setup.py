#!/usr/bin/env python3
"""Одноразовая OAuth-авторизация в Google Calendar.

Запускать НА СВОЁМ КОМПЬЮТЕРЕ (Mac), а не на сервере: скрипт открывает браузер,
где ты входишь в Google-аккаунт и подтверждаешь доступ к календарю. Результат —
файл token.json, который потом копируется на сервер.

    python3 google_auth_setup.py

Требует рядом credentials.json (OAuth client типа "Desktop app" из Google Cloud
Console — см. шаг 2.4 в DEPLOY.md).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
except ImportError:
    sys.exit(
        "Не установлены зависимости Google.\n"
        "Выполни:  pip install google-api-python-client google-auth-oauthlib google-auth-httplib2"
    )

# Полный доступ к календарю: бот и создаёт события, и умеет их удалять
# (нужно, чтобы убрать тестовые события на шаге 5).
SCOPES = ["https://www.googleapis.com/auth/calendar"]

BASE_DIR = Path(__file__).resolve().parent
CREDENTIALS_FILE = BASE_DIR / os.environ.get("GOOGLE_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = BASE_DIR / os.environ.get("GOOGLE_TOKEN_FILE", "token.json")


def main() -> int:
    if not CREDENTIALS_FILE.exists():
        sys.exit(
            f"Не найден {CREDENTIALS_FILE.name}.\n"
            "Скачай его в Google Cloud Console: APIs & Services -> Credentials ->\n"
            "Create credentials -> OAuth client ID -> Application type: Desktop app ->\n"
            f"Download JSON, положи рядом со скриптом под именем {CREDENTIALS_FILE.name}."
        )

    creds: Credentials | None = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if creds and creds.valid:
        print(f"{TOKEN_FILE.name} уже валиден — новая авторизация не нужна.")
    else:
        if creds and creds.expired and creds.refresh_token:
            print("Токен истёк, обновляю по refresh_token...")
            try:
                creds.refresh(Request())
            except Exception as exc:  # refresh_token отозван/протух
                print(f"Обновить не удалось ({exc}). Прохожу авторизацию заново.")
                creds = None
        if not creds or not creds.valid:
            print("Открываю браузер для входа в Google...")
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            # access_type=offline + prompt=consent гарантируют выдачу refresh_token,
            # без которого бот перестанет работать через час.
            creds = flow.run_local_server(
                port=0,
                access_type="offline",
                prompt="consent",
                authorization_prompt_message="Открой ссылку, если браузер не открылся сам:\n{url}",
                success_message="Готово. Можно закрыть вкладку и вернуться в терминал.",
            )

        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
        TOKEN_FILE.chmod(0o600)
        print(f"Сохранил {TOKEN_FILE.name} (права 600).")

    if not creds.refresh_token:
        print(
            "\nВНИМАНИЕ: в токене нет refresh_token — бот проработает ~1 час и отвалится.\n"
            f"Удали {TOKEN_FILE.name} и запусти скрипт ещё раз."
        )
        return 1

    # Проверка боем: читаем список календарей тем же токеном, что получит бот.
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    calendars = service.calendarList().list().execute().get("items", [])
    print(f"\nДоступ подтверждён. Календарей: {len(calendars)}")
    for cal in calendars:
        mark = "  <- primary" if cal.get("primary") else ""
        print(f"  - {cal['summary']}  (id: {cal['id']}){mark}")
    print(
        "\nЕсли писать нужно не в основной календарь — впиши нужный id "
        "в GOOGLE_CALENDAR_ID в .env."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
