"""Application settings loaded from environment / .env via pydantic-settings."""
from functools import cached_property

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: str = "dev"
    app_name: str = "marketplace"
    log_level: str = "INFO"

    host: str = "0.0.0.0"
    port: int = 8000

    postgres_user: str = "marketplace"
    postgres_password: str = "marketplace_dev"
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "marketplace"

    jwt_secret: str = "change-me-in-dev-and-always-rotate-in-prod"
    jwt_alg: str = "HS256"
    jwt_access_ttl_min: int = 15
    jwt_refresh_ttl_days: int = 30

    webauthn_rp_id: str = "localhost"
    webauthn_rp_name: str = "Marketplace V0"
    webauthn_origin: str = "http://localhost:8000"

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-5"

    openai_api_key: str = ""
    openai_embedding_model: str = "text-embedding-3-small"

    self_verifier_url: str = "http://localhost:9000/verify"

    @cached_property
    def database_url_async(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @cached_property
    def database_url_sync(self) -> str:
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


settings = Settings()
