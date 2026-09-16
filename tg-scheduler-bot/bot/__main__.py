"""Точка входа: python -m bot

Поднимает Telegram-приложение, ставит ежедневную задачу и уходит в поллинг.
Логи пишутся в stdout/stderr — их забирает journald (контракт A.2.4).
"""

from __future__ import annotations

import logging
import sys
from datetime import time

from openai import AsyncOpenAI
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
)

from bot.calendars.base import CalendarError
from bot.calendars.google import GoogleCalendar
from bot.calendars.icloud import ICloudCalendar
from bot.config import Config, ConfigError, load_config
from bot.parser import PlanParser
from bot.telegram_bot import SchedulerBot, on_error
from bot.transcribe import Transcriber

logger = logging.getLogger("bot")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    # Библиотеки многословны на DEBUG и умеют печатать заголовки запросов —
    # держим их на WARNING, чтобы в лог не попали токены.
    for noisy in ("httpx", "httpcore", "telegram", "urllib3", "caldav", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def build_application(config: Config) -> Application:
    openai_client = AsyncOpenAI(api_key=config.openai_api_key)

    google = GoogleCalendar(
        calendar_id=config.google_calendar_id,
        timezone_name=config.timezone_name,
        token_file=config.google_token_file,
        service_account_info=config.google_service_account_info,
    )
    if google.uses_service_account:
        logger.info(
            "Google: сервисный аккаунт %s -> календарь %s",
            google.service_account_email,
            config.google_calendar_id,
        )
    else:
        logger.info("Google: OAuth-токен -> календарь %s", config.google_calendar_id)
    icloud = ICloudCalendar(
        url=config.icloud_caldav_url,
        apple_id=config.icloud_apple_id,
        app_password=config.icloud_app_password,
        calendar_name=config.icloud_calendar_name,
    )

    # Проверяем доступ к календарям сразу, чтобы поломка была видна в логе
    # при старте, а не в 08:45 следующего утра. Падать при этом не нужно:
    # временная недоступность сети не повод не отвечать на /start.
    for name, calendar in (("Google Calendar", google), ("iCloud", icloud)):
        try:
            calendar.check()
            logger.info("%s: доступ есть", name)
        except CalendarError as exc:
            logger.error("%s: доступа нет — %s", name, exc)

    scheduler_bot = SchedulerBot(
        config=config,
        parser=PlanParser(
            client=openai_client,
            model=config.openai_model,
            tz=config.timezone,
            timezone_name=config.timezone_name,
            default_minutes=config.default_event_minutes,
        ),
        transcriber=Transcriber(openai_client, config.openai_transcribe_model),
        google=google,
        icloud=icloud,
    )

    # Ограничитель частоты (AIORateLimiter) намеренно не ставим: он нужен
    # ботам с потоком пользователей, требует отдельного extra и здесь был бы
    # лишней зависимостью — бот обслуживает одного человека.
    application = ApplicationBuilder().token(config.telegram_bot_token).build()

    application.add_handler(CommandHandler("start", scheduler_bot.start))
    application.add_handler(CommandHandler("plan", scheduler_bot.plan))
    application.add_handler(CommandHandler("today", scheduler_bot.today))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, scheduler_bot.on_text)
    )
    application.add_handler(
        MessageHandler(filters.VOICE | filters.AUDIO, scheduler_bot.on_voice)
    )
    application.add_error_handler(on_error)

    # Тайм-зона задаётся явно: на сервере системное время в UTC (контракт A.2.3).
    daily_at = time(
        hour=config.daily_prompt_time.hour,
        minute=config.daily_prompt_time.minute,
        tzinfo=config.timezone,
    )
    application.job_queue.run_daily(
        scheduler_bot.daily_question, time=daily_at, name="daily-question"
    )
    logger.info(
        "Ежедневный вопрос запланирован на %s %s",
        daily_at.strftime("%H:%M"),
        config.timezone_name,
    )

    return application


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        # До настройки логирования — значит, прямо в stderr.
        print(str(exc), file=sys.stderr)
        return 1

    setup_logging(config.log_level)
    logger.info("Запускаюсь, тайм-зона %s", config.timezone_name)

    application = build_application(config)
    logger.info("Поллинг Telegram запущен")
    application.run_polling(drop_pending_updates=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
