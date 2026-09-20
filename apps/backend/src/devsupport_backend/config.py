"""Application configuration for the backend foundation."""

from typing import Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from devsupport_backend.target_config import (
    AdapterType,
    InvestigationTargetConfig,
    ProviderConfig,
)


class ProviderBackendConfig(BaseModel):
    """Backend-only settings for one registered provider instance.

    ``ProviderConfig`` selects this object by opaque key.  Endpoint and credential
    values deliberately live only in application deployment settings, never in a
    target capability configuration or runtime tool input.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    backend_config_key: str = Field(min_length=1, max_length=100)
    adapter_type: AdapterType
    endpoint: str | None = Field(default=None, min_length=1, max_length=2_000)
    order_service_url: str | None = Field(default=None, min_length=1, max_length=2_000)
    payment_service_url: str | None = Field(default=None, min_length=1, max_length=2_000)
    credential: SecretStr | None = None

    @field_validator("backend_config_key")
    @classmethod
    def require_opaque_backend_key(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "://" in normalized:
            raise ValueError("backend_config_key must be an opaque deployment reference")
        return normalized

    @model_validator(mode="after")
    def validate_adapter_settings(self) -> "ProviderBackendConfig":
        if self.adapter_type in {AdapterType.OPENSEARCH, AdapterType.PROMETHEUS}:
            if self.endpoint is None:
                raise ValueError(f"{self.adapter_type.value} requires endpoint")
        elif self.adapter_type is AdapterType.FAULT_LAB:
            if self.order_service_url is None or self.payment_service_url is None:
                raise ValueError("fault_lab requires order_service_url and payment_service_url")
        else:
            raise ValueError("backend settings require a registered adapter type")
        return self


class Settings(BaseSettings):
    """Configuration loaded from environment variables when the backend starts."""

    app_name: str = "DevSupport Agent V2"
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
    opensearch_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("OPENSEARCH_URL", "DEVSUPPORT_OPENSEARCH_URL"),
    )
    prometheus_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PROMETHEUS_URL", "DEVSUPPORT_PROMETHEUS_URL"),
    )
    # Compatibility-only global switch; M2.2 will select adapters from target configuration.
    runtime_evidence_provider: Literal["fault_lab", "otel_demo"] = Field(
        default="fault_lab",
        validation_alias=AliasChoices(
            "RUNTIME_EVIDENCE_PROVIDER", "DEVSUPPORT_RUNTIME_EVIDENCE_PROVIDER"
        ),
    )
    investigation_target_configs: list[InvestigationTargetConfig] = Field(
        default_factory=list,
        validation_alias=AliasChoices(
            "INVESTIGATION_TARGET_CONFIGS", "DEVSUPPORT_INVESTIGATION_TARGET_CONFIGS"
        ),
    )
    provider_configs: list[ProviderConfig] = Field(
        default_factory=list,
        validation_alias=AliasChoices("PROVIDER_CONFIGS", "DEVSUPPORT_PROVIDER_CONFIGS"),
    )
    provider_backend_configs: list[ProviderBackendConfig] = Field(
        default_factory=list,
        validation_alias=AliasChoices(
            "PROVIDER_BACKEND_CONFIGS", "DEVSUPPORT_PROVIDER_BACKEND_CONFIGS"
        ),
    )

    model_config = SettingsConfigDict(env_file=".env", env_prefix="DEVSUPPORT_")

    @field_validator("llm_thinking_mode", mode="before")
    @classmethod
    def normalize_blank_llm_thinking_mode(cls, value: object) -> object:
        """Treat an intentionally blank provider-specific mode as unset."""
        if isinstance(value, str) and not value.strip():
            return None
        return value


settings = Settings()
