"""Configuration for the order service."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables."""

    payment_service_url: str = "http://127.0.0.1:8001"
    payment_timeout_seconds: float = Field(default=3.0, gt=0)

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
