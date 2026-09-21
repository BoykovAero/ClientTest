"""Тесты старта: вебхук на токене и его следы в логе (сеть не нужна)."""

from __future__ import annotations

import json
import logging

import pytest

from telegram.error import Conflict, TelegramError

from bot.__main__ import drop_webhook
from bot.telegram_bot import on_error


class FakeWebhookInfo:
    def __init__(self, url: str) -> None:
        self.url = url


class FakeBot:
    def __init__(self, url: str = "", fail_info=None, fail_delete=None) -> None:
        self._url = url
        self._fail_info = fail_info
        self._fail_delete = fail_delete
        self.deleted = False
        self.dropped_pending = None

    async def get_webhook_info(self) -> FakeWebhookInfo:
        if self._fail_info is not None:
            raise self._fail_info
        return FakeWebhookInfo(self._url)

    async def delete_webhook(self, drop_pending_updates: bool = False) -> None:
        if self._fail_delete is not None:
            raise self._fail_delete
        self.deleted = True
        self.dropped_pending = drop_pending_updates


class FakeApplication:
    def __init__(self, bot: FakeBot) -> None:
        self.bot = bot


class FakeContext:
    def __init__(self, error: Exception) -> None:
        self.error = error


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str, **kwargs) -> None:
        self.replies.append(text)


class FakeUpdate:
    def __init__(self, message: FakeMessage) -> None:
        self.message = message


async def test_webhook_snimaetsya_pered_pollingom():
    bot = FakeBot(url="https://chuzhoy.example/hook")
    await drop_webhook(FakeApplication(bot))
    assert bot.deleted
    # Накопленные вебхуком апдейты не нужны: они успели устареть.
    assert bot.dropped_pending is True


async def test_bez_vebhuka_nichego_ne_trogaem():
    bot = FakeBot(url="")
    await drop_webhook(FakeApplication(bot))
    assert not bot.deleted


async def test_nedostupnyy_telegram_ne_ronyaet_start():
    bot = FakeBot(fail_info=TelegramError("timed out"))
    await drop_webhook(FakeApplication(bot))
    assert not bot.deleted


async def test_otkaz_udalit_vebhuk_ne_ronyaet_start():
    bot = FakeBot(url="https://chuzhoy.example/hook", fail_delete=TelegramError("403"))
    await drop_webhook(FakeApplication(bot))


async def test_conflict_pishetsya_odnoy_strokoy_s_prichinoy(caplog):
    message = FakeMessage()
    error = Conflict("can't use getUpdates method while webhook is active")
    with caplog.at_level(logging.ERROR, logger="bot.telegram_bot"):
        await on_error(FakeUpdate(message), FakeContext(error))

    record = caplog.records[-1]
    assert record.exc_info is None, "трассировка забивает лог: Conflict повторяется в каждом цикле"
    assert "вебхук" in record.getMessage()
    # Ошибка поллинга не относится к конкретному сообщению — отвечать некому.
    assert message.replies == []


# ─── iCloud как необязательный календарь ────────────────────────────────


def _env(**overrides) -> dict:
    """Минимальное рабочее окружение; None в overrides убирает переменную."""
    base = {
        "TELEGRAM_BOT_TOKEN": "123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "ALLOWED_USER_ID": "6103930706",
        "OPENAI_API_KEY": "gsk_test",
        "GOOGLE_CALENDAR_ID": "someone@gmail.com",
        "GOOGLE_SERVICE_ACCOUNT_JSON": json.dumps(
            {
                "type": "service_account",
                "client_email": "bot@project.iam.gserviceaccount.com",
                "private_key": "-----BEGIN PRIVATE KEY-----\nZmFrZQ==\n-----END PRIVATE KEY-----\n",
            }
        ),
        "ICLOUD_APPLE_ID": "a@b.c",
        "ICLOUD_APP_PASSWORD": "aaaa-bbbb-cccc-dddd",
    }
    base.update(overrides)
    return {key: value for key, value in base.items() if value is not None}


def _load(monkeypatch, tmp_path, **overrides):
    from bot.config import load_config

    for name in (
        "TELEGRAM_BOT_TOKEN", "ALLOWED_USER_ID", "OPENAI_API_KEY", "GOOGLE_CALENDAR_ID",
        "ICLOUD_APPLE_ID", "ICLOUD_APP_PASSWORD", "GOOGLE_SERVICE_ACCOUNT_JSON",
        "GOOGLE_SERVICE_ACCOUNT_FILE", "OPENAI_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    for key, value in _env(**overrides).items():
        monkeypatch.setenv(key, value)
    return load_config(tmp_path / "нет-такого.env")


def test_icloud_vklyuchen_kogda_est_oba_dostupa(monkeypatch, tmp_path):
    config = _load(monkeypatch, tmp_path)
    assert config.icloud_enabled


def test_bez_dostupov_icloud_prosto_vyklyuchen(monkeypatch, tmp_path):
    config = _load(monkeypatch, tmp_path, ICLOUD_APPLE_ID=None, ICLOUD_APP_PASSWORD=None)
    assert not config.icloud_enabled


def test_polovina_dostupov_eto_oshibka(monkeypatch, tmp_path):
    from bot.config import ConfigError

    with pytest.raises(ConfigError) as caught:
        _load(monkeypatch, tmp_path, ICLOUD_APP_PASSWORD=None)
    assert "ICLOUD_APP_PASSWORD" in str(caught.value)


async def test_bez_icloud_sobytie_pishetsya_tolko_v_google():
    from bot.calendars.base import SaveResult
    from bot.telegram_bot import SchedulerBot

    class FakeGoogle:
        def __init__(self):
            self.saved = []

        def save(self, event):
            self.saved.append(event)
            return SaveResult.created("Google Calendar")

    google = FakeGoogle()
    bot = SchedulerBot(
        config=object(), parser=object(), transcriber=object(), image_reader=object(),
        openai_client=object(), google=google, icloud=None,
    )
    results = await bot._save(object())
    assert [result.target for result in results] == ["Google Calendar"]
    assert len(google.saved) == 1
