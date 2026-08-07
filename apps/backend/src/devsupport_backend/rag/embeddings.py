"""Embedding-provider boundary for knowledge ingestion."""

from collections.abc import Sequence
from typing import Protocol

import httpx

from devsupport_backend.config import Settings


class EmbeddingError(RuntimeError):
    """Raised when an embedding provider cannot return usable vectors."""


class EmbeddingClient(Protocol):
    """Generate one vector for each supplied text, in the same order."""

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return vector representations for ``texts``."""


class OpenAICompatibleEmbeddingClient:
    """Small synchronous adapter for OpenAI-compatible embedding endpoints."""

    def __init__(self, *, model: str, api_key: str, base_url: str) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    @classmethod
    def from_settings(cls, config: Settings) -> "OpenAICompatibleEmbeddingClient":
        """Build a configured provider or raise a clear startup error."""
        if not config.embedding_model:
            raise EmbeddingError("EMBEDDING_MODEL must be configured before ingestion")
        if config.embedding_api_key is None:
            raise EmbeddingError("EMBEDDING_API_KEY must be configured before ingestion")
        return cls(
            model=config.embedding_model,
            api_key=config.embedding_api_key.get_secret_value(),
            base_url=config.embedding_base_url,
        )

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Call the provider once and validate its ordered embedding response."""
        if not texts:
            return []

        try:
            response = httpx.post(
                f"{self._base_url}/embeddings",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"model": self._model, "input": list(texts)},
                timeout=30.0,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise EmbeddingError(f"embedding request failed: {error}") from error

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or len(data) != len(texts):
            raise EmbeddingError("embedding response did not contain one vector per input")

        try:
            ordered = sorted(data, key=lambda item: item["index"])
            vectors = [[float(value) for value in item["embedding"]] for item in ordered]
        except (KeyError, TypeError, ValueError) as error:
            raise EmbeddingError("embedding response has an invalid vector format") from error

        if any(not vector for vector in vectors):
            raise EmbeddingError("embedding response contains an empty vector")
        return vectors
