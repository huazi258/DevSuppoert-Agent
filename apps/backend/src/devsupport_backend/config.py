"""Application configuration for the backend foundation."""

from pydantic import AliasChoices, Field, SecretStr
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
    fault_lab_order_service_url: str = "http://127.0.0.1:8000"
    fault_lab_payment_service_url: str = "http://127.0.0.1:8001"

    model_config = SettingsConfigDict(env_file=".env", env_prefix="DEVSUPPORT_")


settings = Settings()
