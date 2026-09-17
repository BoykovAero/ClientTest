"""Тесты чистых функций setup_env.py (сеть не нужна)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from setup_env import (  # noqa: E402
    check_openai_key,
    check_telegram_token,
    mask,
    parse_env,
    render_env,
)


class TestParseEnv:
    def test_missing_file_gives_empty(self, tmp_path):
        assert parse_env(tmp_path / "absent.env") == {}

    def test_reads_pairs_and_skips_comments(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text(
            "# комментарий\n\nTELEGRAM_BOT_TOKEN=123:ABC\nALLOWED_USER_ID=42\n",
            encoding="utf-8",
        )
        assert parse_env(env) == {"TELEGRAM_BOT_TOKEN": "123:ABC", "ALLOWED_USER_ID": "42"}

    def test_value_with_colons_is_kept_whole(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("ICLOUD_CALDAV_URL=https://caldav.icloud.com\n", encoding="utf-8")
        assert parse_env(env)["ICLOUD_CALDAV_URL"] == "https://caldav.icloud.com"


class TestRenderEnv:
    TEMPLATE = "# Telegram\nTELEGRAM_BOT_TOKEN=\nALLOWED_USER_ID=\n\n# Прочее\nLOG_LEVEL=INFO\n"

    def test_substitutes_values(self):
        result = render_env(self.TEMPLATE, {"TELEGRAM_BOT_TOKEN": "123:ABC"})
        assert "TELEGRAM_BOT_TOKEN=123:ABC" in result

    def test_keeps_comments(self):
        result = render_env(self.TEMPLATE, {})
        assert "# Telegram" in result
        assert "# Прочее" in result

    def test_keeps_defaults_for_untouched_keys(self):
        result = render_env(self.TEMPLATE, {"TELEGRAM_BOT_TOKEN": "x"})
        assert "LOG_LEVEL=INFO" in result

    def test_empty_value_clears_the_key(self):
        result = render_env(self.TEMPLATE, {"LOG_LEVEL": ""})
        assert "LOG_LEVEL=\n" in result


class TestMask:
    def test_short_secret_fully_hidden(self):
        assert mask("abc") == "***"
        assert "a" not in mask("abcd1234")

    def test_long_secret_shows_edges_only(self):
        masked = mask("sk-proj-1234567890abcdef")
        assert masked.startswith("sk-p")
        assert masked.endswith("cdef")
        assert "1234567890" not in masked


class TestValidatorsRejectBadInputWithoutNetwork:
    """Явно кривые значения отсеиваются до обращения к сети."""

    @pytest.mark.parametrize("token", ["", "не-токен", "12345", "abc:def"])
    def test_malformed_telegram_token(self, token):
        passed, message = check_telegram_token(token)
        assert not passed
        assert "токен" in message

    @pytest.mark.parametrize("key", ["", "proj-123", "Bearer sk-1"])
    def test_malformed_openai_key(self, key):
        passed, message = check_openai_key(key)
        assert not passed
        assert "sk-" in message


class TestProviderDetection:
    """Ключ узнаётся по префиксу, отсюда берутся адрес и модели."""

    def test_openai_key(self):
        from setup_env import detect_provider

        name, base_url = detect_provider("sk-proj-abc123")
        assert name == "OpenAI"
        assert base_url == "https://api.openai.com/v1"

    def test_groq_key(self):
        from setup_env import detect_provider

        name, base_url = detect_provider("gsk_abc123")
        assert name == "Groq"
        assert base_url == "https://api.groq.com/openai/v1"

    def test_unknown_key(self):
        from setup_env import detect_provider

        assert detect_provider("просто-строка") == (None, None)

    def test_validator_names_both_prefixes(self):
        from setup_env import check_openai_key

        passed, message = check_openai_key("непонятно-что")
        assert not passed
        assert "sk-" in message and "gsk_" in message
