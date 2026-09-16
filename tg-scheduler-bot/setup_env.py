#!/usr/bin/env python3
"""Интерактивная сборка .env с проверкой каждого значения.

Запускать НА СВОЁМ КОМПЬЮТЕРЕ (Mac), в каталоге проекта:

    python3 setup_env.py

Скрипт спрашивает ключи по одному, каждый сразу проверяет обращением к
настоящему сервису и записывает .env с правами 600. Уже заполненные значения
можно оставить, не вводя заново.

Где брать каждый ключ — DEPLOY.md, шаги 1–4.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from getpass import getpass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
TEMPLATE_FILE = BASE_DIR / ".env.example"
TIMEOUT = 20

BOLD, DIM, GREEN, RED, YELLOW, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[0m"
)


def say(text: str = "") -> None:
    print(text)


def ok(text: str) -> None:
    print(f"  {GREEN}✓{RESET} {text}")


def bad(text: str) -> None:
    print(f"  {RED}✗{RESET} {text}")


def warn(text: str) -> None:
    print(f"  {YELLOW}!{RESET} {text}")


# ─── работа с .env ──────────────────────────────────────────────────────────
def parse_env(path: Path) -> dict[str, str]:
    """Читает KEY=VALUE, игнорируя комментарии и пустые строки."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def render_env(template: str, values: dict[str, str]) -> str:
    """Подставляет значения в шаблон .env.example, сохраняя комментарии."""
    out = []
    for line in template.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key in values:
                out.append(f"{key}={values[key]}")
                continue
        out.append(line)
    return "\n".join(out) + "\n"


def mask(value: str) -> str:
    """Маскирует секрет для показа на экране."""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * 6}{value[-4:]}"


# ─── проверки ───────────────────────────────────────────────────────────────
def _get_json(url: str, headers: dict[str, str] | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except Exception:
            return exc.code, {}


def check_telegram_token(token: str) -> tuple[bool, str]:
    if not re.fullmatch(r"\d{6,}:[\w-]{30,}", token):
        return False, "не похоже на токен (ожидается 123456789:AAH...)"
    try:
        status, payload = _get_json(f"https://api.telegram.org/bot{token}/getMe")
    except Exception as exc:
        return False, f"нет связи с Telegram: {exc}"
    if status == 401:
        return False, "Telegram не принял токен (401). Проверь или перевыпусти у @BotFather"
    if status != 200 or not payload.get("ok"):
        return False, f"Telegram ответил {status}"
    bot = payload.get("result", {})
    return True, f"бот @{bot.get('username', '?')} ({bot.get('first_name', '')})"


def check_telegram_user(token: str, user_id: str) -> tuple[bool, str]:
    """Шлёт пробное сообщение — так сразу видно, что id верный и чат открыт."""
    if not user_id.isdigit():
        return False, "id должен быть числом (возьми у @userinfobot)"
    url = (
        f"https://api.telegram.org/bot{token}/sendMessage"
        f"?chat_id={user_id}&text=" + urllib.parse.quote("Проверка связи: настройка идёт верно.")
    )
    try:
        status, payload = _get_json(url)
    except Exception as exc:
        return False, f"нет связи с Telegram: {exc}"
    if status == 200 and payload.get("ok"):
        return True, "пробное сообщение отправлено — проверь Telegram"
    description = payload.get("description", f"код {status}")
    if "chat not found" in description.lower():
        return False, "чат не найден: открой своего бота в Telegram и нажми /start, потом повтори"
    return False, description


def check_openai_key(key: str) -> tuple[bool, str]:
    if not key.startswith("sk-"):
        return False, "ключ OpenAI начинается с sk-"
    try:
        status, payload = _get_json(
            "https://api.openai.com/v1/models", {"Authorization": f"Bearer {key}"}
        )
    except Exception as exc:
        return False, f"нет связи с OpenAI: {exc}"
    if status == 401:
        return False, "OpenAI не принял ключ (401)"
    if status == 429:
        return False, "ключ принят, но исчерпана квота — пополни баланс (DEPLOY.md, шаг 2)"
    if status != 200:
        return False, f"OpenAI ответил {status}"
    return True, f"ключ рабочий, моделей доступно: {len(payload.get('data', []))}"


def check_icloud(apple_id: str, password: str, calendar_name: str) -> tuple[bool, str]:
    if not re.fullmatch(r"[a-z]{4}-[a-z]{4}-[a-z]{4}-[a-z]{4}", password.strip()):
        warn("пароль не в формате xxxx-xxxx-xxxx-xxxx — это точно app-specific password?")
    try:
        import caldav
    except ImportError:
        return False, "не установлен caldav: pip install -r requirements.txt"

    try:
        client = caldav.DAVClient(
            url="https://caldav.icloud.com", username=apple_id, password=password
        )
        calendars = client.principal().calendars()
    except Exception as exc:
        text = str(exc)
        if "401" in text or "Unauthorized" in text:
            return False, "401: нужен app-specific password с appleid.apple.com, не обычный пароль"
        return False, f"нет связи с iCloud: {text[:120]}"

    names = [str(calendar.name) for calendar in calendars]
    if calendar_name and calendar_name not in names:
        return False, f"календаря {calendar_name!r} нет. Доступны: {', '.join(names)}"
    target = calendar_name or (names[0] if names else "?")
    return True, f"пишем в календарь {target!r} (всего календарей: {len(names)})"


# ─── диалог ─────────────────────────────────────────────────────────────────
def ask(label: str, current: str, secret: bool, hint: str) -> str:
    """Спрашивает значение; пустой ввод оставляет текущее."""
    say()
    say(f"{BOLD}{label}{RESET}")
    say(f"{DIM}{hint}{RESET}")
    if current:
        shown = mask(current) if secret else current
        prompt = f"  Сейчас: {shown}\n  Новое значение (Enter — оставить): "
    else:
        prompt = "  Значение: "
    entered = (getpass(prompt) if secret else input(prompt)).strip()
    return entered or current


def main() -> int:
    if not TEMPLATE_FILE.exists():
        say(f"Нет {TEMPLATE_FILE.name} — запускай скрипт из каталога проекта.")
        return 1

    say(f"{BOLD}Настройка ТГ-планировщика{RESET}")
    say("Каждое значение проверяется сразу. Где его взять — DEPLOY.md, шаги 1–4.")
    say("Прервать можно в любой момент: Ctrl+C, введённое до этого не сохранится.")

    values = parse_env(ENV_FILE)
    failures: list[str] = []

    # 1. Telegram
    values["TELEGRAM_BOT_TOKEN"] = ask(
        "1/5. Токен бота", values.get("TELEGRAM_BOT_TOKEN", ""), True,
        "Telegram -> @BotFather -> /newbot -> строка вида 8123456789:AAH...",
    )
    passed, detail = check_telegram_token(values["TELEGRAM_BOT_TOKEN"])
    (ok if passed else bad)(detail)
    if not passed:
        failures.append("TELEGRAM_BOT_TOKEN")

    # 2. user id — проверяется только при рабочем токене
    values["ALLOWED_USER_ID"] = ask(
        "2/5. Твой Telegram user id", values.get("ALLOWED_USER_ID", ""), False,
        "Telegram -> @userinfobot -> /start -> число из поля Id",
    )
    if passed:
        sent, detail = check_telegram_user(
            values["TELEGRAM_BOT_TOKEN"], values["ALLOWED_USER_ID"]
        )
        (ok if sent else bad)(detail)
        if not sent:
            failures.append("ALLOWED_USER_ID")
    else:
        warn("пропускаю проверку: сначала нужен рабочий токен")
        failures.append("ALLOWED_USER_ID")

    # 3. OpenAI
    values["OPENAI_API_KEY"] = ask(
        "3/5. Ключ OpenAI", values.get("OPENAI_API_KEY", ""), True,
        "platform.openai.com/api-keys -> Create new secret key. Баланс должен быть > 0",
    )
    passed, detail = check_openai_key(values["OPENAI_API_KEY"])
    (ok if passed else bad)(detail)
    if not passed:
        failures.append("OPENAI_API_KEY")

    # 4. iCloud
    values["ICLOUD_APPLE_ID"] = ask(
        "4/5. Apple ID", values.get("ICLOUD_APPLE_ID", ""), False,
        "Почта, на которую заведён Apple ID",
    )
    values["ICLOUD_APP_PASSWORD"] = ask(
        "     Пароль для приложения", values.get("ICLOUD_APP_PASSWORD", ""), True,
        "appleid.apple.com -> Sign-In and Security -> App-Specific Passwords -> +",
    )
    values["ICLOUD_CALENDAR_NAME"] = ask(
        "     Имя календаря", values.get("ICLOUD_CALENDAR_NAME", ""), False,
        "Пусто — календарь по умолчанию. Календарь должен быть в разделе iCloud, не «На моём Mac»",
    )
    passed, detail = check_icloud(
        values["ICLOUD_APPLE_ID"],
        values["ICLOUD_APP_PASSWORD"],
        values["ICLOUD_CALENDAR_NAME"],
    )
    (ok if passed else bad)(detail)
    if not passed:
        failures.append("ICLOUD_APP_PASSWORD")

    # 5. Google — отдельным скриптом, тут только статус
    say()
    say(f"{BOLD}5/5. Google Calendar{RESET}")
    token_file = BASE_DIR / values.get("GOOGLE_TOKEN_FILE", "token.json")
    credentials_file = BASE_DIR / values.get("GOOGLE_CREDENTIALS_FILE", "credentials.json")
    if token_file.exists():
        ok(f"{token_file.name} на месте")
    elif credentials_file.exists():
        warn(f"есть {credentials_file.name}, но нет {token_file.name}")
        say("     Выполни:  python3 google_auth_setup.py")
        failures.append("token.json")
    else:
        bad(f"нет ни {credentials_file.name}, ни {token_file.name}")
        say("     Пройди DEPLOY.md, шаг 4, затем:  python3 google_auth_setup.py")
        failures.append("token.json")

    # ─── запись ─────────────────────────────────────────────────────────────
    ENV_FILE.write_text(
        render_env(TEMPLATE_FILE.read_text(encoding="utf-8"), values), encoding="utf-8"
    )
    ENV_FILE.chmod(0o600)

    say()
    say(f"{BOLD}Записал .env (права 600).{RESET}")
    if failures:
        say(f"{YELLOW}Осталось поправить: {', '.join(dict.fromkeys(failures))}{RESET}")
        say("Запусти скрипт ещё раз — введённые значения подставятся сами.")
        return 1

    say(f"{GREEN}Все проверки пройдены.{RESET} Дальше — DEPLOY.md, шаг 6 (сервер) и шаг 7 (деплой).")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        say("\nПрервано, .env не изменён.")
        raise SystemExit(130)
