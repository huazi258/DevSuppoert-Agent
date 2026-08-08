"""Tests for the OpenAI-compatible LLM boundary."""

import httpx
import pytest

from devsupport_backend.agent.llm import LLMError, OpenAICompatibleLLMClient
from devsupport_backend.config import Settings


def _settings(monkeypatch: pytest.MonkeyPatch, **environment: str) -> Settings:
    for name in (
        "LLM_MODEL",
        "DEVSUPPORT_LLM_MODEL",
        "LLM_API_KEY",
        "DEVSUPPORT_LLM_API_KEY",
        "LLM_BASE_URL",
        "DEVSUPPORT_LLM_BASE_URL",
        "LLM_TIMEOUT_SECONDS",
        "DEVSUPPORT_LLM_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    return Settings()


def test_llm_timeout_defaults_to_ninety_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _settings(monkeypatch).llm_timeout_seconds == 90.0


def test_llm_timeout_accepts_both_environment_variable_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _settings(monkeypatch, LLM_TIMEOUT_SECONDS="45").llm_timeout_seconds == 45.0
    monkeypatch.delenv("LLM_TIMEOUT_SECONDS")
    monkeypatch.setenv("DEVSUPPORT_LLM_TIMEOUT_SECONDS", "75")
    assert Settings().llm_timeout_seconds == 75.0


def test_client_uses_configured_connect_and_read_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _settings(
        monkeypatch,
        LLM_MODEL="test-model",
        LLM_API_KEY="test-key",
        LLM_TIMEOUT_SECONDS="45",
    )
    captured: dict[str, object] = {}

    def fake_post(*args: object, **kwargs: object) -> httpx.Response:
        captured.update(kwargs)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "completed"}}]},
            request=httpx.Request("POST", "https://example.test/chat/completions"),
        )

    monkeypatch.setattr(httpx, "post", fake_post)

    assert OpenAICompatibleLLMClient.from_settings(config).complete(
        system_prompt="system", user_prompt="user"
    ) == "completed"

    timeout = captured["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == 10.0
    assert timeout.read == 45.0


def test_read_timeout_is_wrapped_without_exposing_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _settings(
        monkeypatch,
        LLM_MODEL="test-model",
        LLM_API_KEY="secret-value",
        LLM_TIMEOUT_SECONDS="45",
    )

    def fake_post(*args: object, **kwargs: object) -> httpx.Response:
        raise httpx.ReadTimeout("network stalled")

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(LLMError, match="read timed out after 45 seconds") as error:
        OpenAICompatibleLLMClient.from_settings(config).complete(
            system_prompt="system", user_prompt="user"
        )

    assert "secret-value" not in str(error.value)
