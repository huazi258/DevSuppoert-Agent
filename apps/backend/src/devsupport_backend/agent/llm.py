"""Small OpenAI-compatible chat-completion boundary for Agent nodes."""

from __future__ import annotations

from typing import Literal, Protocol

import httpx

from devsupport_backend.config import Settings


class LLMError(RuntimeError):
    """Raised when an LLM provider cannot return a usable completion."""


class LLMClient(Protocol):
    """Generate one text completion from trusted node instructions and context."""

    def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        """Return the provider's response content as text."""


class OpenAICompatibleLLMClient:
    """Synchronous adapter for an OpenAI-compatible chat-completions endpoint."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        timeout_seconds: float,
        thinking_mode: Literal["enabled", "disabled"] | None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._thinking_mode = thinking_mode
        self._timeout = httpx.Timeout(
            connect=10.0,
            read=timeout_seconds,
            write=timeout_seconds,
            pool=timeout_seconds,
        )

    @classmethod
    def from_settings(cls, config: Settings) -> "OpenAICompatibleLLMClient":
        """Build a configured client without exposing secret values."""
        if not config.llm_model:
            raise LLMError("LLM_MODEL must be configured before hypothesis generation")
        if config.llm_api_key is None or not config.llm_api_key.get_secret_value():
            raise LLMError("LLM_API_KEY must be configured before hypothesis generation")
        return cls(
            model=config.llm_model,
            api_key=config.llm_api_key.get_secret_value(),
            base_url=config.llm_base_url,
            timeout_seconds=config.llm_timeout_seconds,
            thinking_mode=config.llm_thinking_mode,
        )

    def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        """Call the provider and extract one non-empty assistant message."""
        from devsupport_backend.agent.budget import (
            ActiveExecutionBudgetExceeded,
            effective_llm_timeout_seconds,
            llm_timeout_is_budget_limited,
        )

        effective_timeout_seconds = effective_llm_timeout_seconds(self._timeout_seconds)
        request_body: dict[str, object] = {
            "model": self._model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if self._thinking_mode is not None:
            request_body["thinking"] = {"type": self._thinking_mode}
        try:
            response = httpx.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=request_body,
                timeout=_httpx_timeout(effective_timeout_seconds),
            )
            response.raise_for_status()
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
        except httpx.ReadTimeout as error:
            if llm_timeout_is_budget_limited(
                self._timeout_seconds, effective_timeout_seconds
            ):
                raise ActiveExecutionBudgetExceeded(
                    "active execution budget expired while waiting for the LLM provider"
                ) from error
            raise LLMError(
                f"LLM request read timed out after {effective_timeout_seconds:g} seconds"
            ) from error
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as error:
            raise LLMError(f"LLM request failed: {error}") from error

        if not isinstance(content, str) or not content.strip():
            raise LLMError("LLM response did not contain non-empty message content")
        return content


def _httpx_timeout(timeout_seconds: float) -> httpx.Timeout:
    """Build one bounded timeout without widening the configured provider limit."""
    return httpx.Timeout(
        connect=min(10.0, timeout_seconds),
        read=timeout_seconds,
        write=timeout_seconds,
        pool=timeout_seconds,
    )
