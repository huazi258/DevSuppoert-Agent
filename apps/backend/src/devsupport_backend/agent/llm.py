"""Small OpenAI-compatible chat-completion boundary for Agent nodes."""

from __future__ import annotations

from typing import Protocol

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
        self, *, model: str, api_key: str, base_url: str, timeout_seconds: float
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
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
        )

    def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        """Call the provider and extract one non-empty assistant message."""
        try:
            response = httpx.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self._model,
                    "temperature": 0,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                },
                timeout=self._timeout,
            )
            response.raise_for_status()
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
        except httpx.ReadTimeout as error:
            raise LLMError(
                f"LLM request read timed out after {self._timeout_seconds:g} seconds"
            ) from error
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as error:
            raise LLMError(f"LLM request failed: {error}") from error

        if not isinstance(content, str) or not content.strip():
            raise LLMError("LLM response did not contain non-empty message content")
        return content
