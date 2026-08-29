"""Tests for the OpenAI-compatible LLM boundary."""

import httpx
import pytest
from pydantic import ValidationError

from devsupport_backend.agent.budget import (
    ActiveExecutionBudgetExceeded,
    InvestigationBudget,
    active_execution_scope,
)
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
        "LLM_THINKING_MODE",
        "DEVSUPPORT_LLM_THINKING_MODE",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    return Settings()


def test_llm_timeout_defaults_to_fifty_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _settings(monkeypatch).llm_timeout_seconds == 50.0


def test_llm_timeout_accepts_both_environment_variable_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _settings(monkeypatch, LLM_TIMEOUT_SECONDS="45").llm_timeout_seconds == 45.0
    monkeypatch.delenv("LLM_TIMEOUT_SECONDS")
    monkeypatch.setenv("DEVSUPPORT_LLM_TIMEOUT_SECONDS", "75")
    assert Settings().llm_timeout_seconds == 75.0


def test_llm_thinking_mode_defaults_to_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _settings(monkeypatch).llm_thinking_mode is None


def test_blank_llm_thinking_mode_is_normalized_to_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _settings(monkeypatch, LLM_THINKING_MODE="").llm_thinking_mode is None


def test_llm_thinking_mode_rejects_invalid_values(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        _settings(monkeypatch, LLM_THINKING_MODE="automatic")


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


@pytest.mark.parametrize(
    ("thinking_mode", "expected_thinking"),
    [
        (None, None),
        ("disabled", {"type": "disabled"}),
        ("enabled", {"type": "enabled"}),
    ],
)
def test_client_adds_thinking_only_when_configured(
    monkeypatch: pytest.MonkeyPatch,
    thinking_mode: str | None,
    expected_thinking: dict[str, str] | None,
) -> None:
    environment = {"LLM_MODEL": "test-model", "LLM_API_KEY": "test-key"}
    if thinking_mode is not None:
        environment["LLM_THINKING_MODE"] = thinking_mode
    config = _settings(monkeypatch, **environment)
    captured: dict[str, object] = {}

    def fake_post(*args: object, **kwargs: object) -> httpx.Response:
        captured.update(kwargs)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "completed"}}]},
            request=httpx.Request("POST", "https://example.test/chat/completions"),
        )

    monkeypatch.setattr(httpx, "post", fake_post)

    OpenAICompatibleLLMClient.from_settings(config).complete(
        system_prompt="system", user_prompt="user"
    )

    request_body = captured["json"]
    assert isinstance(request_body, dict)
    if expected_thinking is None:
        assert "thinking" not in request_body
    else:
        assert request_body["thinking"] == expected_thinking


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


def test_client_limits_provider_timeout_to_remaining_active_execution_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _settings(
        monkeypatch,
        LLM_MODEL="test-model",
        LLM_API_KEY="test-key",
        LLM_TIMEOUT_SECONDS="50",
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
    def clock() -> float:
        return 0.0

    with active_execution_scope(
        75.0, InvestigationBudget(max_active_execution_seconds=95.0), clock=clock
    ):
        assert OpenAICompatibleLLMClient.from_settings(config).complete(
            system_prompt="system", user_prompt="user"
        ) == "completed"

    timeout = captured["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == 15.0


def test_budget_limited_provider_timeout_has_typed_manual_path_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _settings(
        monkeypatch,
        LLM_MODEL="test-model",
        LLM_API_KEY="test-key",
        LLM_TIMEOUT_SECONDS="50",
    )

    def fake_post(*args: object, **kwargs: object) -> httpx.Response:
        raise httpx.ReadTimeout("controlled")

    monkeypatch.setattr(httpx, "post", fake_post)
    with active_execution_scope(
        75.0,
        InvestigationBudget(max_active_execution_seconds=95.0),
        clock=lambda: 0.0,
    ):
        with pytest.raises(ActiveExecutionBudgetExceeded):
            OpenAICompatibleLLMClient.from_settings(config).complete(
                system_prompt="system", user_prompt="user"
            )


def test_client_does_not_invoke_provider_when_active_budget_has_no_llm_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _settings(monkeypatch, LLM_MODEL="test-model", LLM_API_KEY="test-key")
    provider_called = False

    def fake_post(*args: object, **kwargs: object) -> httpx.Response:
        nonlocal provider_called
        provider_called = True
        raise AssertionError("provider must not be invoked")

    monkeypatch.setattr(httpx, "post", fake_post)
    with active_execution_scope(
        90.0,
        InvestigationBudget(max_active_execution_seconds=95.0),
        clock=lambda: 0.0,
    ):
        with pytest.raises(ActiveExecutionBudgetExceeded):
            OpenAICompatibleLLMClient.from_settings(config).complete(
                system_prompt="system", user_prompt="user"
            )

    assert provider_called is False


def test_empty_content_is_rejected_even_with_reasoning_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _settings(monkeypatch, LLM_MODEL="test-model", LLM_API_KEY="secret-value")

    def fake_post(*args: object, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "", "reasoning_content": "private reasoning"}}
                ]
            },
            request=httpx.Request("POST", "https://example.test/chat/completions"),
        )

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(LLMError, match="non-empty message content") as error:
        OpenAICompatibleLLMClient.from_settings(config).complete(
            system_prompt="system", user_prompt="user"
        )

    assert "secret-value" not in str(error.value)
