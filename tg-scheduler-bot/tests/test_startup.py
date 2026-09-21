"""Тесты старта: вебхук на токене и его следы в логе (сеть не нужна)."""

from __future__ import annotations

import logging

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
