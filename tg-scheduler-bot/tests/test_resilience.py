"""Бот должен подниматься, даже когда календарь недоступен.

Телеграм — единственный способ узнать, что с ботом не так. Если старт
падает из-за Google, человек видит молчание и больше ничего, а хостинг
перезапускает бота по кругу: так он умирает насовсем.
"""

from __future__ import annotations

import pytest

from bot.__main__ import _prepare_calendars
from bot.calendars.base import CalendarError
from bot.calendars.google import GoogleCalendar


class Config:
    google_calendar_id = "me@gmail.com"
    timezone_name = "Europe/Moscow"


class BrokenGoogle:
    """Календарь, который отказывает всеми известными способами."""

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.uses_service_account = True

    @property
    def service_account_email(self) -> str:
        raise self._error

    def ensure_calendars(self, categories, share_with):
        raise self._error


# RefreshError от google-auth — обычное исключение, не CalendarError:
# отозванный ключ, отключённый сервисный аккаунт, недоступный Google.
BREAKAGES = [
    CalendarError("403: ключу закрыт доступ"),
    RuntimeError("RefreshError: invalid_grant"),
    OSError("сеть недоступна"),
    ValueError("ключ сервисного аккаунта испорчен"),
]


@pytest.mark.parametrize("error", BREAKAGES, ids=lambda e: type(e).__name__)
def test_podgotovka_kalendarey_ne_ronyaet_start(error):
    # Не бросает — значит, сборка бота дойдёт до Telegram.
    _prepare_calendars(Config(), BrokenGoogle(error))


@pytest.mark.parametrize("error", BREAKAGES, ids=lambda e: type(e).__name__)
def test_prichina_vidna_v_loge(error, caplog):
    with caplog.at_level("ERROR"):
        _prepare_calendars(Config(), BrokenGoogle(error))
    assert any("календар" in record.message.lower() for record in caplog.records)


# ─── отказ ключа приходит как CalendarError ─────────────────────────────


class Boom:
    """Учётные данные, которые не обновляются."""

    valid = False

    def refresh(self, request):
        raise RuntimeError("RefreshError: account disabled")


def google_with(credentials) -> GoogleCalendar:
    calendar = GoogleCalendar(
        calendar_id="me@gmail.com",
        timezone_name="Europe/Moscow",
        token_file=None,
        service_account_info={"client_email": "bot@p.iam.gserviceaccount.com"},
    )
    calendar._credentials_cache = credentials
    return calendar


def test_otkaz_klyucha_eto_calendar_error():
    """Иначе он уходит мимо обработчиков и валит процесс целиком."""
    with pytest.raises(CalendarError):
        google_with(Boom())._credentials()


def test_ensure_calendars_ne_vypuskaet_syroe_isklyuchenie():
    """Сборка сервиса раньше стояла вне try и выпускала RefreshError."""
    with pytest.raises(CalendarError):
        google_with(Boom()).ensure_calendars(["Учёба"], share_with="me@gmail.com")
