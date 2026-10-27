from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"), extra="ignore", case_sensitive=False
    )

    environment: str = "development"
    api_base_url: str = "http://localhost:8000"
    cors_origins: str = "http://localhost:3000"

    # Defaults to a local SQLite file so the stack boots before Neon is wired up.
    database_url: str = "sqlite+aiosqlite:///./fluxora.db"
    db_echo: bool = False

    jwt_secret: str = "dev-only-insecure-secret"
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 30
    refresh_token_ttl_days: int = 30

    playback_signing_private_key: str = ""
    playback_signing_key_id: str = ""

    # S3-compatible object storage: MinIO locally, Cloudflare R2 or AWS S3 in production.
    # One backend serves all three — only the endpoint and credentials differ.
    s3_endpoint: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_bucket: str = "fluxora"
    s3_region: str = "us-east-1"
    # MinIO serves buckets as a path segment rather than a subdomain.
    s3_force_path_style: bool = True
    # Presigned URLs must be signed for the host the client will call. When the API talks
    # to MinIO over a Docker network name but the browser reaches it on localhost, set
    # this to the browser-facing origin.
    s3_public_endpoint: str = ""
    s3_create_bucket: bool = True

    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    worker_concurrency: int = 2
    worker_poll_interval_seconds: float = 2.0
    media_scratch_dir: str = "./tmp"

    # ---- LLM / agent ----
    llm_provider: str = "openai"          # openai | anthropic | stub
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5"
    llm_temperature: float = 0.0
    llm_timeout_seconds: float = 90.0
    llm_max_retries: int = 2

    langsmith_api_key: str = ""
    langsmith_project: str = "fluxora"
    langsmith_tracing: bool = False
    langsmith_endpoint: str = "https://api.smith.langchain.com"

    # ---- Agent guardrails ----
    # Hard ceilings, enforced in code rather than asked of the model: a planner that
    # ignores its instructions must still not be able to run away.
    agent_max_plan_steps: int = 8
    agent_max_tool_calls: int = 12
    agent_tool_timeout_seconds: float = 900.0
    agent_max_runs_per_hour: int = 60
    agent_max_prompt_chars: int = 8000
    agent_require_approval: bool = True

    # Optional Guardrails AI Hub validators, by name. Each must be installed first with
    # `guardrails hub install hub://<name>`; missing ones are skipped with a warning so
    # a deployment without Hub access still runs on the local validators.
    # e.g. "guardrails/detect_jailbreak,guardrails/toxic_language"
    guardrails_input_validator_names: str = ""
    # e.g. "guardrails/detect_pii,guardrails/toxic_language"
    guardrails_output_validator_names: str = ""

    # ---- Redis / BullMQ (agent tool dispatch) ----
    redis_url: str = "redis://localhost:6379/0"
    agent_tool_queue: str = "fluxora-agent-tools"
    agent_tool_concurrency: int = 4
    agent_tool_attempts: int = 2

    otel_exporter_otlp_endpoint: str = ""
    otel_service_name: str = "fluxora-api"

    upload_default_timeout_seconds: int = 3600
    upload_max_timeout_seconds: int = 604800
    test_asset_max_duration_seconds: float = 10.0
    max_list_limit: int = 100
    default_list_limit: int = 25

    webhook_retry_schedule_seconds: list[int] = Field(
        default=[60, 300, 1800, 7200, 21600, 86400]
    )
    webhook_tolerance_seconds: int = 300

    @property
    def async_database_url(self) -> str:
        """Normalise a pasted Neon/libpq connection string into an asyncpg URL.

        Neon hands out `postgresql://…?sslmode=require&channel_binding=require`. Those are
        libpq parameters: asyncpg does not understand them and raises on connect, so the
        driver is pinned and the params are stripped here rather than making every operator
        hand-edit the URL. TLS is not lost — it is re-applied via `connect_args` below.
        """
        url = self.database_url
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+asyncpg://", 1)
        if "+asyncpg" not in url or "?" not in url:
            return url
        base, _, query = url.partition("?")
        kept = [
            param
            for param in query.split("&")
            if param.split("=")[0] not in {"sslmode", "channel_binding", "options"}
        ]
        return f"{base}?{'&'.join(kept)}" if kept else base

    @property
    def requires_tls(self) -> bool:
        """True when the original URL asked for TLS (as every Neon URL does)."""
        return "sslmode=require" in self.database_url or "neon.tech" in self.database_url

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def storage_configured(self) -> bool:
        """True when real object storage is wired up, i.e. not the local-disk fallback."""
        return bool(
            self.s3_access_key_id and self.s3_secret_access_key and self.s3_endpoint
        )

    @property
    def guardrails_input_validators(self) -> list[str]:
        return [n.strip() for n in self.guardrails_input_validator_names.split(",") if n.strip()]

    @property
    def guardrails_output_validators(self) -> list[str]:
        return [n.strip() for n in self.guardrails_output_validator_names.split(",") if n.strip()]

    @property
    def llm_configured(self) -> bool:
        if self.llm_provider == "openai":
            return bool(self.openai_api_key)
        if self.llm_provider == "anthropic":
            return bool(self.anthropic_api_key)
        return self.llm_provider == "stub"

    @property
    def redis_options(self) -> dict:
        return {"connection": self.redis_url}

    @property
    def s3_signing_endpoint(self) -> str:
        """Endpoint used when generating presigned URLs for clients."""
        return self.s3_public_endpoint or self.s3_endpoint

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
