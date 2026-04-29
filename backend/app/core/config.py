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

    # Embedding service knobs (4.2). Backend = "openai" | "fake"; tests
    # flip to "fake" via env var so the suite stays hermetic. Cache size
    # 1000 ≈ 6MB RAM at 1536-dim float32. TTL bounds memory leak in
    # long-running processes — embeddings are stable for the same text,
    # the TTL is a hygiene measure, not freshness.
    embedding_backend: str = "openai"
    embedding_cache_size: int = 1000
    embedding_cache_ttl_seconds: int = 86_400  # 24h
    # Per-call retry: 3 attempts with exponential backoff (2s, 4s, 8s).
    embedding_max_retries: int = 3
    embedding_retry_min_wait_seconds: float = 2.0
    embedding_retry_max_wait_seconds: float = 10.0

    # Dev-only endpoints (e.g. /api/_dev/embedding-stats). Off in prod.
    enable_dev_endpoints: bool = False

    self_verifier_url: str = "https://api.self.xyz/v1/verify"
    self_verifier_scope: str = "marketplace-it-v0"
    self_verifier_timeout_seconds: float = 10.0

    # V0 stub for the agent-keypair custody seam. Real KMS (AWS/GCP) is V1.
    # File-based: per-agent JSON in `.secrets/agent_keys/<agent_id>.json`.
    # Path is relative to the repo root (cwd at uvicorn boot).
    kms_keys_dir: str = ".secrets/agent_keys"

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
