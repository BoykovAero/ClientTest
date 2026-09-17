"""Тесты чтения и валидации конфигурации."""

from __future__ import annotations

import json
from datetime import time
from pathlib import Path

import pytest

from bot.config import ConfigError, load_config

REQUIRED = {
    "TELEGRAM_BOT_TOKEN": "8123456789:AAHfakefakefakefakefakefakefakefake",
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
        "OPENAI_BASE_URL",
        "OPENAI_TRANSCRIBE_MODEL",
        "ICLOUD_CALDAV_URL",
        "ICLOUD_CALENDAR_NAME",
        "GOOGLE_CREDENTIALS_FILE",
        "GOOGLE_TOKEN_FILE",
        "GOOGLE_SERVICE_ACCOUNT_FILE",
        "GOOGLE_SERVICE_ACCOUNT_JSON",
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


class TestGoogleAuthMode:
    """Доступ к Google: либо сервисный аккаунт, либо OAuth-токен."""

    KEY = {
        "type": "service_account",
        "client_email": "bot@proj.iam.gserviceaccount.com",
        "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
    }

    def test_defaults_to_oauth_token(self, env):
        config = env()
        assert config.google_service_account_info is None
        assert config.google_token_file.exists()

    def test_key_from_env_var_needs_no_files(self, monkeypatch, tmp_path, env):
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", json.dumps(self.KEY))
        monkeypatch.setenv("GOOGLE_CALENDAR_ID", "me@gmail.com")
        monkeypatch.setenv("GOOGLE_TOKEN_FILE", str(tmp_path / "absent.json"))

        config = env()
        assert config.google_service_account_info["client_email"] == self.KEY["client_email"]

    def test_key_from_file(self, monkeypatch, tmp_path, env):
        key_file = tmp_path / "service-account.json"
        key_file.write_text(json.dumps(self.KEY), encoding="utf-8")
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", str(key_file))
        monkeypatch.setenv("GOOGLE_CALENDAR_ID", "me@gmail.com")
        monkeypatch.setenv("GOOGLE_TOKEN_FILE", str(tmp_path / "absent.json"))

        assert env().google_service_account_info["client_email"] == self.KEY["client_email"]

    def test_env_var_wins_over_file(self, monkeypatch, tmp_path, env):
        key_file = tmp_path / "service-account.json"
        key_file.write_text(json.dumps({**self.KEY, "client_email": "file@x.com"}), encoding="utf-8")
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", str(key_file))
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", json.dumps(self.KEY))
        monkeypatch.setenv("GOOGLE_CALENDAR_ID", "me@gmail.com")

        assert env().google_service_account_info["client_email"] == self.KEY["client_email"]

    def test_broken_json_rejected(self, monkeypatch, env):
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{не json")
        monkeypatch.setenv("GOOGLE_CALENDAR_ID", "me@gmail.com")
        with pytest.raises(ConfigError, match="JSON"):
            env()

    def test_key_without_private_key_rejected(self, monkeypatch, env):
        monkeypatch.setenv(
            "GOOGLE_SERVICE_ACCOUNT_JSON",
            json.dumps({"client_email": "bot@proj.iam.gserviceaccount.com"}),
        )
        monkeypatch.setenv("GOOGLE_CALENDAR_ID", "me@gmail.com")
        with pytest.raises(ConfigError, match="private_key"):
            env()

    def test_missing_key_file_rejected(self, monkeypatch, tmp_path, env):
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", str(tmp_path / "nope.json"))
        monkeypatch.setenv("GOOGLE_CALENDAR_ID", "me@gmail.com")
        with pytest.raises(ConfigError, match="сервисного аккаунта"):
            env()

    def test_service_account_with_primary_is_rejected(self, monkeypatch, env):
        """primary у сервисного аккаунта — его собственный пустой календарь."""
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", json.dumps(self.KEY))
        monkeypatch.setenv("GOOGLE_CALENDAR_ID", "primary")
        with pytest.raises(ConfigError, match="primary"):
            env()

    def test_neither_mode_configured_is_rejected(self, monkeypatch, tmp_path, env):
        monkeypatch.setenv("GOOGLE_TOKEN_FILE", str(tmp_path / "absent.json"))
        with pytest.raises(ConfigError, match="GOOGLE_SERVICE_ACCOUNT_JSON"):
            env()


class TestServiceAccountAsBase64:
    """Панели хостингов иногда калечат многострочные значения — принимаем base64."""

    KEY = TestGoogleAuthMode.KEY

    def test_base64_is_accepted(self, monkeypatch, env):
        import base64

        encoded = base64.b64encode(json.dumps(self.KEY).encode("utf-8")).decode("ascii")
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", encoded)
        monkeypatch.setenv("GOOGLE_CALENDAR_ID", "me@gmail.com")

        assert env().google_service_account_info["client_email"] == self.KEY["client_email"]

    def test_plain_json_still_works(self, monkeypatch, env):
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", json.dumps(self.KEY))
        monkeypatch.setenv("GOOGLE_CALENDAR_ID", "me@gmail.com")

        assert env().google_service_account_info["client_email"] == self.KEY["client_email"]

    def test_json_with_real_newlines_survives(self, monkeypatch, env):
        """Именно так выглядит скачанный из консоли файл — с отступами."""
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", json.dumps(self.KEY, indent=2))
        monkeypatch.setenv("GOOGLE_CALENDAR_ID", "me@gmail.com")

        assert env().google_service_account_info["client_email"] == self.KEY["client_email"]

    def test_garbage_names_both_formats(self, monkeypatch, env):
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "совершенно не то")
        monkeypatch.setenv("GOOGLE_CALENDAR_ID", "me@gmail.com")
        with pytest.raises(ConfigError, match="base64"):
            env()


class TestOpenAIBaseUrl:
    """Провайдер задаётся адресом: пусто — OpenAI, иначе совместимый сервис."""

    def test_defaults_to_openai(self, env):
        assert env().openai_base_url == "https://api.openai.com/v1"

    def test_blank_value_becomes_openai(self, monkeypatch, env):
        """SDK сам читает OPENAI_BASE_URL — пустая строка дала бы пустой адрес."""
        monkeypatch.setenv("OPENAI_BASE_URL", "   ")
        assert env().openai_base_url == "https://api.openai.com/v1"

    def test_accepts_groq(self, monkeypatch, env):
        monkeypatch.setenv("OPENAI_BASE_URL", "https://api.groq.com/openai/v1")
        assert env().openai_base_url == "https://api.groq.com/openai/v1"

    def test_rejects_non_https(self, monkeypatch, env):
        monkeypatch.setenv("OPENAI_BASE_URL", "api.groq.com/openai/v1")
        with pytest.raises(ConfigError, match="OPENAI_BASE_URL"):
            env()


class TestTelegramTokenFormat:
    """Заглушка или опечатка в токене должна ловиться на старте."""

    def test_accepts_real_shape(self, monkeypatch, env):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "8080076954:AAHKfH5D3bXeYUd76pHiyavxrDVHKNxJeeM")
        assert env().telegram_bot_token.startswith("8080076954:")

    @pytest.mark.parametrize(
        "token",
        [
            "твой_токен_от_botfather",   # вставленная заглушка
            "your_token_here",
            "8080076954",                 # без секретной части
            ":AAHKfH5D3bXeYUd76pHiyavx",  # без id
            "8080076954:short",           # секретная часть слишком коротка
        ],
    )
    def test_rejects_placeholders_and_typos(self, monkeypatch, env, token):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
        with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
            env()

    def test_message_does_not_echo_the_value(self, monkeypatch, env):
        """Сообщение об ошибке не должно печатать сам токен."""
        secret = "8080076954:AAHKfH5D3bXeYUd76pHiyavxrDVHKNxJee"
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", secret + "!!!")
        with pytest.raises(ConfigError) as excinfo:
            env()
        assert secret not in str(excinfo.value)
