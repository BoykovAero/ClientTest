"""Тесты расшифровки ошибок провайдера (сеть не нужна)."""

from __future__ import annotations

import pytest
from openai import OpenAIError

from bot.llm import MODELS_SHOWN, describe


class FakeError(OpenAIError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code
        self._message = message

    def __str__(self) -> str:
        return self._message


class FakeModel:
    def __init__(self, model_id: str):
        self.id = model_id


class FakeModels:
    def __init__(self, ids, fail=False):
        self._ids = ids
        self._fail = fail

    async def list(self):
        if self._fail:
            raise FakeError("нет связи")
        return type("Response", (), {"data": [FakeModel(i) for i in self._ids]})()


class FakeClient:
    def __init__(self, ids=(), fail=False):
        self.models = FakeModels(list(ids), fail)


MODEL_NOT_FOUND = (
    "Error code: 404 - {'error': {'message': 'The model `llama-3.3-70b-versatile` "
    "does not exist or you do not have access to it.', 'code': 'model_not_found'}}"
)


class TestModelNotFound:
    @pytest.mark.asyncio
    async def test_lists_available_models(self):
        client = FakeClient(["openai/gpt-oss-120b", "qwen/qwen3.6-27b"])
        message = await describe(FakeError(MODEL_NOT_FOUND, 404), client, "llama-3.3-70b-versatile")
        assert "llama-3.3-70b-versatile" in message
        assert "openai/gpt-oss-120b" in message
        assert "qwen/qwen3.6-27b" in message

    @pytest.mark.asyncio
    async def test_truncates_long_lists(self):
        client = FakeClient([f"model-{i:02d}" for i in range(30)])
        message = await describe(FakeError(MODEL_NOT_FOUND, 404), client, "нет-такой")
        assert "и ещё 18" in message

    @pytest.mark.asyncio
    async def test_survives_failure_to_list(self):
        """Если список получить не удалось, сообщение всё равно осмысленное."""
        client = FakeClient(fail=True)
        message = await describe(FakeError(MODEL_NOT_FOUND, 404), client, "нет-такой")
        assert "недоступна" in message


class TestOtherStatuses:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status,fragment",
        [(401, "отозван"), (429, "лимит"), (503, "перегружен")],
    )
    async def test_known_statuses(self, status, fragment):
        message = await describe(FakeError("что-то", status), FakeClient(), "m")
        assert fragment in message

    @pytest.mark.asyncio
    async def test_unknown_error_is_trimmed_to_one_line(self):
        message = await describe(FakeError("первая строка\nвторая", None), FakeClient(), "m")
        assert message == "первая строка"

    @pytest.mark.asyncio
    async def test_403_also_lists_models(self):
        """Тариф может закрывать модель — статус другой, растерянность та же."""
        client = FakeClient(["qwen/qwen3.6-27b"])
        message = await describe(FakeError("Forbidden", 403), client, "openai/gpt-oss-120b")
        assert "недоступна" in message
        assert "qwen/qwen3.6-27b" in message

    @pytest.mark.asyncio
    async def test_existing_model_refused_is_called_a_plan_limit(self):
        """Модель в списке есть, а доступ закрыт — дело не в имени."""
        client = FakeClient(["openai/gpt-oss-120b", "qwen/qwen3.6-27b"])
        message = await describe(FakeError("Forbidden", 403), client, "openai/gpt-oss-120b")
        assert "тарифом" in message
        # и всё равно подсказывает, чем заменить
        assert "qwen/qwen3.6-27b" in message

    @pytest.mark.asyncio
    async def test_refused_model_is_not_offered_as_its_own_replacement(self):
        client = FakeClient(["openai/gpt-oss-120b", "qwen/qwen3.6-27b"])
        message = await describe(FakeError("Forbidden", 403), client, "openai/gpt-oss-120b")
        suggestion = message.split("Попробуй другую:", 1)[1]
        assert "gpt-oss-120b" not in suggestion

    @pytest.mark.asyncio
    async def test_sole_refused_model_has_nothing_to_suggest(self):
        client = FakeClient(["openai/gpt-oss-120b"])
        message = await describe(FakeError("Forbidden", 403), client, "openai/gpt-oss-120b")
        assert "тарифом" in message
        assert "Попробуй другую" not in message

    @pytest.mark.asyncio
    async def test_falls_back_when_list_unavailable(self):
        message = await describe(FakeError("Forbidden", 403), FakeClient(fail=True), "m")
        assert "403" in message


class TestAuthErrorsStayAuthErrors:
    @pytest.mark.asyncio
    async def test_401_does_not_list_models(self):
        message = await describe(FakeError("Unauthorized", 401), FakeClient(["a", "b"]), "m")
        assert "Доступны" not in message
        assert "отозван" in message


def test_models_shown_is_sane():
    assert 5 <= MODELS_SHOWN <= 30
