"""Точка входа: python -m bot

Поднимает Telegram-приложение, ставит ежедневную задачу и уходит в поллинг.
Логи пишутся в stdout/stderr — их забирает journald (контракт A.2.4).
"""

from __future__ import annotations

import logging
import sys
from datetime import time

from openai import AsyncOpenAI
from telegram import Update
from telegram.error import InvalidToken, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from bot.calendars.base import CalendarError
from bot.calendars.google import GoogleCalendar
from bot.calendars.icloud import ICloudCalendar
from bot.config import Config, ConfigError, load_config
from bot.parser import PlanParser
from bot.telegram_bot import SchedulerBot, clear_webhook, on_error
from bot.transcribe import Transcriber
from bot.sheets import SheetsReader
from bot.vision import ImageReader

logger = logging.getLogger("bot")

# Сколько ждать ответа модели на одну часть расписания, секунд.
REQUEST_TIMEOUT = 90.0


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


async def drop_webhook(application: Application) -> None:
    """Снимает вебхук с токена перед тем, как уйти в поллинг.

    Telegram отдаёт апдейты либо в вебхук, либо через getUpdates, но никогда
    обоими способами сразу: при активном вебхуке getUpdates отвечает Conflict.
    Бот при этом остаётся живым процессом и для хостинга выглядит исправным,
    а на сообщения не отвечает — молча, потому что не видит их. Снимаем
    вебхук сами, иначе такое состояние чинится только руками.
    """
    try:
        url = await clear_webhook(application.bot, drop_pending=False)
    except TelegramError as exc:
        logger.warning("Не удалось проверить вебхук: %s", exc)
        return

    if url is not None:
        logger.warning("На токене стоял вебхук %s — снял, он ломает поллинг", url)


def build_application(config: Config) -> Application:
    # Адрес передаётся всегда: SDK сам читает OPENAI_BASE_URL из окружения,
    # и пустая переменная там дала бы клиент с пустым адресом.
    # Таймаут обязателен: по умолчанию SDK ждёт десять минут, и один
    # зависший запрос держит разбор всего расписания.
    openai_client = AsyncOpenAI(
        api_key=config.openai_api_key,
        base_url=config.openai_base_url,
        timeout=REQUEST_TIMEOUT,
        max_retries=1,
    )
    logger.info(
        "Модели: разбор %s, распознавание %s, зрение %s (%s)",
        config.openai_model,
        config.openai_transcribe_model,
        config.openai_vision_model,
        config.openai_base_url,
    )

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
    # iCloud подключается, только если заданы доступы. Без него события
    # пишутся в один Google — так в приложении календаря не появляется
    # вторая копия каждой записи.
    icloud = None
    if config.icloud_enabled:
        icloud = ICloudCalendar(
            url=config.icloud_caldav_url,
            apple_id=config.icloud_apple_id,
            app_password=config.icloud_app_password,
            calendar_name=config.icloud_calendar_name,
        )
    else:
        logger.info("iCloud выключен: события пойдут только в Google Calendar")

    # Проверяем доступ к календарям сразу, чтобы поломка была видна в логе
    # при старте, а не в 08:45 следующего утра. Падать при этом не нужно:
    # временная недоступность сети не повод не отвечать на /start.
    targets = [("Google Calendar", google)]
    if icloud is not None:
        targets.append(("iCloud", icloud))
    for name, calendar in targets:
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
        image_reader=ImageReader(openai_client, config.openai_vision_model),
        openai_client=openai_client,
        sheets=SheetsReader(config.google_service_account_info),
        google=google,
        icloud=icloud,
    )

    # Ограничитель частоты (AIORateLimiter) намеренно не ставим: он нужен
    # ботам с потоком пользователей, требует отдельного extra и здесь был бы
    # лишней зависимостью — бот обслуживает одного человека.
    application = (
        ApplicationBuilder()
        .token(config.telegram_bot_token)
        .post_init(drop_webhook)
        .build()
    )

    application.add_handler(CommandHandler("start", scheduler_bot.start))
    application.add_handler(CommandHandler("plan", scheduler_bot.plan))
    application.add_handler(CommandHandler("today", scheduler_bot.today))
    application.add_handler(CommandHandler("models", scheduler_bot.models))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, scheduler_bot.on_text)
    )
    application.add_handler(
        MessageHandler(filters.VOICE | filters.AUDIO, scheduler_bot.on_voice)
    )
    application.add_handler(
        MessageHandler(filters.PHOTO | filters.Document.ALL, scheduler_bot.on_document)
    )
    application.add_handler(
        CallbackQueryHandler(scheduler_bot.on_decision, pattern=r"^(save|drop):")
    )
    application.add_handler(CallbackQueryHandler(scheduler_bot.on_pick, pattern=r"^pick:"))
    application.add_handler(
        CallbackQueryHandler(scheduler_bot.on_delete, pattern=r"^(kill|keep):")
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
    try:
        # Список типов обновлений задаём явно. Если его не передать, Telegram
        # берёт тот, что остался от последнего setWebhook на этом токене, —
        # а он может быть урезанным. Без callback_query бот перестаёт видеть
        # нажатия на кнопки: сообщения доходят, а «Записать» и «Удалить» не
        # работают, причём молча.
        # Накопленное не выбрасываем. Перезапуск случается при каждом
        # деплое и при любой возне хостинга с контейнером, и всё, что
        # человек отправил в эту минуту, пропадало бы молча. Telegram
        # хранит очередь сутки, так что после долгого простоя бот разберёт
        # и то, что пришло, пока его не было.
        application.run_polling(
            drop_pending_updates=False, allowed_updates=Update.ALL_TYPES
        )
    except InvalidToken:
        # Своё сообщение вместо исключения библиотеки: та подставляет в текст
        # сам токен, и он оседает в логах хостинга.
        logger.error(
            "Telegram отверг токен. Проверь TELEGRAM_BOT_TOKEN: "
            "он мог быть отозван через /revoke у @BotFather — тогда нужен новый."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
