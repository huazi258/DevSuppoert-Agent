"""Application configuration for the backend foundation."""

from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration loaded from environment variables when the backend starts."""

    app_name: str = "DevSupport Agent V0"
    app_environment: str = "local"
    database_url: str = "postgresql+psycopg://devsupport:devsupport@127.0.0.1:15432/devsupport"
    embedding_model: str | None = Field(
        default=None,
        validation_alias=AliasChoices("EMBEDDING_MODEL", "DEVSUPPORT_EMBEDDING_MODEL"),
    )
    embedding_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("EMBEDDING_API_KEY", "DEVSUPPORT_EMBEDDING_API_KEY"),
    )
    embedding_base_url: str = Field(
        default="https://api.openai.com/v1",
        validation_alias=AliasChoices("EMBEDDING_BASE_URL", "DEVSUPPORT_EMBEDDING_BASE_URL"),
    )
    llm_model: str | None = Field(
        default=None,
        validation_alias=AliasChoices("LLM_MODEL", "DEVSUPPORT_LLM_MODEL"),
    )
    llm_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("LLM_API_KEY", "DEVSUPPORT_LLM_API_KEY"),
    )
    llm_base_url: str = Field(
        default="https://api.openai.com/v1",
        validation_alias=AliasChoices("LLM_BASE_URL", "DEVSUPPORT_LLM_BASE_URL"),
    )
    llm_timeout_seconds: float = Field(
        default=50.0,
        gt=0,
        validation_alias=AliasChoices(
            "LLM_TIMEOUT_SECONDS", "DEVSUPPORT_LLM_TIMEOUT_SECONDS"
        ),
    )
    llm_thinking_mode: Literal["enabled", "disabled"] | None = Field(
        default=None,
        validation_alias=AliasChoices("LLM_THINKING_MODE", "DEVSUPPORT_LLM_THINKING_MODE"),
    )
    fault_lab_order_service_url: str = "http://127.0.0.1:8000"
    fault_lab_payment_service_url: str = "http://127.0.0.1:8001"

    model_config = SettingsConfigDict(env_file=".env", env_prefix="DEVSUPPORT_")

    @field_validator("llm_thinking_mode", mode="before")
    @classmethod
    def normalize_blank_llm_thinking_mode(cls, value: object) -> object:
        """Treat an intentionally blank provider-specific mode as unset."""
        if isinstance(value, str) and not value.strip():
            return None
        return value


settings = Settings()
