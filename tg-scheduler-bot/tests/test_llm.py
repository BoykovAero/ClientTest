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
    async def test_404_without_model_word_is_not_treated_as_model_error(self):
        message = await describe(FakeError("Not Found", 404), FakeClient(["a"]), "m")
        assert "Доступны" not in message


def test_models_shown_is_sane():
    assert 5 <= MODELS_SHOWN <= 30
