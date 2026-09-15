"""Тесты чтения и валидации конфигурации."""

from __future__ import annotations

from datetime import time
from pathlib import Path

import pytest

from bot.config import ConfigError, load_config

REQUIRED = {
    "TELEGRAM_BOT_TOKEN": "8123456789:AAHtest",
    "ALLOWED_USER_ID": "412345678",
    "OPENAI_API_KEY": "sk-test",
    "ICLOUD_APPLE_ID": "user@icloud.com",
    "ICLOUD_APP_PASSWORD": "abcd-efgh-ijkl-mnop",
}


@pytest.fixture
def env(monkeypatch, tmp_path: Path):
    """Чистое окружение с обязательными переменными и существующим token.json."""
    for name in list(REQUIRED) + [
        "OPENAI_MODEL",
        "OPENAI_TRANSCRIBE_MODEL",
        "ICLOUD_CALDAV_URL",
        "ICLOUD_CALENDAR_NAME",
        "GOOGLE_CREDENTIALS_FILE",
        "GOOGLE_TOKEN_FILE",
        "GOOGLE_CALENDAR_ID",
        "TIMEZONE",
        "DAILY_PROMPT_TIME",
        "DEFAULT_EVENT_MINUTES",
        "LOG_LEVEL",
    ]:
        monkeypatch.delenv(name, raising=False)

    for name, value in REQUIRED.items():
        monkeypatch.setenv(name, value)

    token = tmp_path / "token.json"
    token.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("GOOGLE_TOKEN_FILE", str(token))

    # .env не существует — значения берём только из окружения
    return lambda: load_config(env_file=tmp_path / "absent.env")


class TestDefaults:
    def test_applies_documented_defaults(self, env):
        config = env()
        assert config.timezone_name == "Europe/Moscow"
        assert config.daily_prompt_time == time(8, 45)
        assert config.default_event_minutes == 60
        assert config.google_calendar_id == "primary"
        assert config.openai_model == "gpt-4o-mini"
        assert config.openai_transcribe_model == "whisper-1"
        assert config.log_level == "INFO"

    def test_reads_required_values(self, env):
        config = env()
        assert config.telegram_bot_token == REQUIRED["TELEGRAM_BOT_TOKEN"]
        assert config.allowed_user_id == 412345678
        assert config.icloud_apple_id == "user@icloud.com"


class TestOverrides:
    def test_daily_prompt_time(self, monkeypatch, env):
        monkeypatch.setenv("DAILY_PROMPT_TIME", "07:05")
        assert env().daily_prompt_time == time(7, 5)

    def test_timezone(self, monkeypatch, env):
        monkeypatch.setenv("TIMEZONE", "Europe/Berlin")
        assert env().timezone_name == "Europe/Berlin"

    def test_log_level_is_uppercased(self, monkeypatch, env):
        monkeypatch.setenv("LOG_LEVEL", "debug")
        assert env().log_level == "DEBUG"


class TestValidation:
    def test_missing_required_lists_every_name(self, monkeypatch, env):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
        monkeypatch.delenv("OPENAI_API_KEY")
        with pytest.raises(ConfigError) as excinfo:
            env()
        message = str(excinfo.value)
        assert "TELEGRAM_BOT_TOKEN" in message
        assert "OPENAI_API_KEY" in message

    def test_blank_value_counts_as_missing(self, monkeypatch, env):
        monkeypatch.setenv("ICLOUD_APP_PASSWORD", "   ")
        with pytest.raises(ConfigError, match="ICLOUD_APP_PASSWORD"):
            env()

    def test_non_numeric_user_id_rejected(self, monkeypatch, env):
        monkeypatch.setenv("ALLOWED_USER_ID", "@boykov")
        with pytest.raises(ConfigError, match="ALLOWED_USER_ID"):
            env()

    def test_malformed_time_rejected(self, monkeypatch, env):
        monkeypatch.setenv("DAILY_PROMPT_TIME", "восемь сорок пять")
        with pytest.raises(ConfigError, match="DAILY_PROMPT_TIME"):
            env()

    def test_unknown_timezone_rejected(self, monkeypatch, env):
        monkeypatch.setenv("TIMEZONE", "Europe/Atlantis")
        with pytest.raises(ConfigError, match="TIMEZONE"):
            env()

    def test_missing_google_token_rejected(self, monkeypatch, tmp_path, env):
        monkeypatch.setenv("GOOGLE_TOKEN_FILE", str(tmp_path / "nope.json"))
        with pytest.raises(ConfigError, match="google_auth_setup"):
            env()

    def test_negative_duration_rejected(self, monkeypatch, env):
        monkeypatch.setenv("DEFAULT_EVENT_MINUTES", "-30")
        with pytest.raises(ConfigError, match="DEFAULT_EVENT_MINUTES"):
            env()
