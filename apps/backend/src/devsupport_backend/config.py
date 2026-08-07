"""Application configuration for the backend foundation."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration loaded from environment variables when the backend starts."""

    app_name: str = "DevSupport Agent V0"
    app_environment: str = "local"

    model_config = SettingsConfigDict(env_file=".env", env_prefix="DEVSUPPORT_")


settings = Settings()
