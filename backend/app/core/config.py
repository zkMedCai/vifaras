"""Application settings loaded from environment / .env via pydantic-settings."""
from functools import cached_property

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
    app_version: str = "0.1.0"
    log_level: str = "INFO"

    host: str = "0.0.0.0"
    port: int = 8000

    postgres_user: str = "marketplace"
    postgres_password: str = "marketplace_dev"
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "marketplace"

    # JWT signing secrets ([7.4.3]). Two-secret overlap window for zero-downtime
    # rotation: `current` signs every new token, `previous` is the optional
    # fallback that verifies still-valid tokens issued before the rotation.
    # Empty `previous` means no rotation in progress — verify happens against
    # `current` only. Bootstrap procedure: docs/JWT_ROTATION_PROCEDURE.md.
    jwt_secret_current: str = "change-me-in-dev-and-always-rotate-in-prod"
    jwt_secret_previous: str = ""
    jwt_alg: str = "HS256"
    jwt_access_ttl_min: int = 15
    refresh_token_ttl_days: int = 30  # opaque, DB-backed since [7.4.2]

    webauthn_rp_id: str = "localhost"
    webauthn_rp_name: str = "Marketplace V0"
    webauthn_origin: str = "http://localhost:3000"

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

    # Match scheduler (4.3): in-process apscheduler refreshing low-match
    # intents periodically. On by default for production; the FastAPI
    # lifespan starts/stops it, so unit tests that don't trigger lifespan
    # never see it. Set to False to disable in environments that don't
    # want the background tick (e.g. CLI scripts, batch tools).
    enable_match_scheduler: bool = True
    match_scheduler_interval_minutes: int = 5
    # Per-tick batch ceiling. Bounded so a single tick never spends more
    # than ~50× embedding cost; the rest waits for the next interval.
    match_scheduler_batch_size: int = 50
    # Threshold below which an intent is considered "match-starved" and
    # eligible for re-scan. 3 matches per intent is the V0 default.
    match_scheduler_min_matches: int = 3

    self_verifier_url: str = "https://api.self.xyz/v1/verify"
    self_verifier_scope: str = "marketplace-it-v0"
    self_verifier_timeout_seconds: float = 10.0

    # KMS envelope encryption ([7.4.1]). Per-agent ed25519 privkeys are
    # AES-256-GCM-encrypted at rest in `kms_agent_keys`; this is the master
    # key as a base64-encoded 32-byte value. Validated at lifespan startup —
    # missing or wrong-size = hard fail (no soft default). Bootstrap with
    # `openssl rand -base64 32`. V0.5+ replaces the env var with cloud KMS
    # Encrypt/Decrypt; the master key never enters the process.
    kms_master_key: str = ""

    # Agent scheduler (6.3.c): in-process apscheduler that ticks agents
    # with pending work. Disabled by default in tests (lifespan-driven so
    # `http_client` won't start it); enabled in production via env var.
    enable_agent_scheduler: bool = False
    # How often the discovery loop runs. 60s is the V0 baseline cadence.
    agent_scheduler_interval_seconds: int = 60
    # Hard cap on candidates returned per discovery cycle. Bounds work
    # even if the rate limiter is misconfigured.
    agent_scheduler_max_candidates: int = 50
    # Concurrency throttle: at most N ticks running simultaneously.
    # Bounded by both the AsyncAnthropic client's concurrency tolerance
    # and the sync verifier's pool size.
    agent_scheduler_max_concurrent: int = 5
    # Sliding-window cap: at most N tick dispatches in any 60s window.
    # Pairs with `_max_concurrent`: concurrent caps spike, per-minute
    # caps sustained throughput.
    agent_scheduler_max_per_minute: int = 30
    # Per-agent cooldown: never tick the same agent more often than this.
    # Belt-and-suspenders next to the rate limiter — applied at discovery
    # so cooldown'd agents don't even reach the dispatcher.
    agent_scheduler_cooldown_seconds: int = 30
    # Stale-intent threshold: agents idle longer than this with active
    # intents get a periodic refresh tick (low-priority signal).
    agent_scheduler_stale_hours: int = 6
    # Daily kill-switch (HARD CAP, global): when today's cumulative spend
    # across all users >= this value, the scheduler stops dispatching for
    # the rest of the UTC day. Protects against runaway/infinite-loop
    # bugs that would burn through the Anthropic budget. Hit = system-wide
    # outage until UTC midnight reset. V0 conservative default; bump in
    # production once cost patterns stabilise.
    max_daily_llm_cost_usd: float = 50.0
    # Per-user soft cap: when an individual user's spend today >= this
    # value, the scheduler skips that user's tick. Other users continue
    # normally. Protects against single-user blow-up scenarios (e.g. an
    # agent stuck in a tool-use loop on one user's intent). Reset at UTC
    # midnight (the daily_cost_tracking row keys on UTC date).
    daily_user_cost_cap_usd: float = 0.50
    # Per-tick circuit breaker inside AgentOrchestrator itself. Unlike
    # the daily caps above (scheduler pre-dispatch), this applies to any
    # direct orchestrator entry point too: CLI smoke scripts, dev hooks,
    # future manual tick APIs. 0 disables the breaker.
    agent_tick_cost_cap_usd: float = 0.10

    # CORS (7.0): JSON origin list in env, e.g.
    # `CORS_ALLOWED_ORIGINS=["https://app.example.com"]`.
    cors_allowed_origins: list[str] = ["http://localhost:3000"]

    # Rate limiting (7.0): slowapi-driven. Off in tests by default; on in
    # production. Test cases that exercise the limiter flip the flag via
    # monkeypatch.
    enable_rate_limiting: bool = False
    rate_limit_default: str = "100/minute"  # IP-keyed, applied to every route
    rate_limit_post_strict: str = "30/minute"  # generic POST writes
    rate_limit_mandate_critical: str = "10/minute"  # mandate / identity / step-up
    rate_limit_self_verifier: str = "5/minute"  # external-call cost protection
    rate_limit_health: str = "60/minute"  # public, polling-friendly
    # 7.1: deep coverage tier — auth endpoints (IP-keyed) and per-user
    # buckets for authenticated routes. `auth_strict` for register
    # (anti-enumeration), `auth_normal` for login (UX-friendlier),
    # `auth_refresh` for token rotation, `user_read` for GET endpoints.
    rate_limit_auth_strict: str = "5/minute"
    rate_limit_auth_normal: str = "10/minute"
    rate_limit_auth_refresh: str = "30/minute"
    rate_limit_user_read: str = "100/minute"

    # Abuse detection (7.1.5). Sequential-email registration: a same-prefix
    # email pattern (e.g. john1@/john2@/john3@) repeated from the same IP
    # within the window emits a SEQUENTIAL_EMAIL_DETECTED audit row.
    # Inclusive threshold: trigger at the Nth attempt where N == threshold.
    abuse_sequential_email_threshold: int = 3
    abuse_sequential_email_window_hours: int = 24

    # OpenTelemetry tracing (7.2.3). Off by default — auto-instrumentation
    # of FastAPI/SQLAlchemy/HTTPX plus manual spans on agent ticks. When
    # disabled, the global tracer remains the SDK NoOp and manual span
    # context managers run essentially free.
    #   - exporter "console": SimpleSpanProcessor + ConsoleSpanExporter.
    #     Synchronous, verbose JSON to stdout. Dev / one-shot inspection.
    #   - exporter "otlp": BatchSpanProcessor + OTLP gRPC exporter to a
    #     local collector (Tempo / Jaeger / Datadog agent). Production.
    telemetry_enabled: bool = False
    telemetry_exporter: str = "console"
    telemetry_otlp_endpoint: str = "http://localhost:4317"

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
