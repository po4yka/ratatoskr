from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from ._secret_marker import SECRET_MARKER


class WebSearchConfig(BaseModel):
    """Web search enrichment configuration for LLM summarization."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    enabled: bool = Field(
        default=False,
        validation_alias="WEB_SEARCH_ENABLED",
        description="Enable web search enrichment for summaries (opt-in)",
    )
    max_queries: int = Field(
        default=3,
        validation_alias="WEB_SEARCH_MAX_QUERIES",
        description="Maximum search queries per article",
    )
    min_content_length: int = Field(
        default=500,
        validation_alias="WEB_SEARCH_MIN_CONTENT_LENGTH",
        description="Minimum content length (chars) to trigger search",
    )
    timeout_sec: float = Field(
        default=10.0,
        validation_alias="WEB_SEARCH_TIMEOUT_SEC",
        description="Timeout for search operations in seconds",
    )
    max_context_chars: int = Field(
        default=2000,
        validation_alias="WEB_SEARCH_MAX_CONTEXT_CHARS",
        description="Maximum characters for injected search context",
    )
    cache_ttl_sec: int = Field(
        default=3600,
        validation_alias="WEB_SEARCH_CACHE_TTL_SEC",
        description="Cache TTL for search results in seconds",
    )

    @field_validator("max_queries", mode="before")
    @classmethod
    def _validate_max_queries(cls, value: Any) -> int:
        if value in (None, ""):
            return 3
        try:
            parsed = int(str(value))
        except ValueError as exc:
            msg = "Max queries must be a valid integer"
            raise ValueError(msg) from exc
        if parsed < 1 or parsed > 10:
            msg = "Max queries must be between 1 and 10"
            raise ValueError(msg)
        return parsed

    @field_validator("min_content_length", mode="before")
    @classmethod
    def _validate_min_content_length(cls, value: Any) -> int:
        if value in (None, ""):
            return 500
        try:
            parsed = int(str(value))
        except ValueError as exc:
            msg = "Min content length must be a valid integer"
            raise ValueError(msg) from exc
        if parsed < 0 or parsed > 10000:
            msg = "Min content length must be between 0 and 10000"
            raise ValueError(msg)
        return parsed

    @field_validator("timeout_sec", mode="before")
    @classmethod
    def _validate_timeout_sec(cls, value: Any) -> float:
        if value in (None, ""):
            return 10.0
        try:
            parsed = float(str(value))
        except ValueError as exc:
            msg = "Timeout must be a valid number"
            raise ValueError(msg) from exc
        if parsed < 1.0 or parsed > 60.0:
            msg = "Timeout must be between 1 and 60 seconds"
            raise ValueError(msg)
        return parsed

    @field_validator("max_context_chars", mode="before")
    @classmethod
    def _validate_max_context_chars(cls, value: Any) -> int:
        if value in (None, ""):
            return 2000
        try:
            parsed = int(str(value))
        except ValueError as exc:
            msg = "Max context chars must be a valid integer"
            raise ValueError(msg) from exc
        if parsed < 500 or parsed > 10000:
            msg = "Max context chars must be between 500 and 10000"
            raise ValueError(msg)
        return parsed

    @field_validator("cache_ttl_sec", mode="before")
    @classmethod
    def _validate_cache_ttl_sec(cls, value: Any) -> int:
        if value in (None, ""):
            return 3600
        try:
            parsed = int(str(value))
        except ValueError as exc:
            msg = "Cache TTL must be a valid integer"
            raise ValueError(msg) from exc
        if parsed < 60 or parsed > 86400:
            msg = "Cache TTL must be between 60 and 86400 seconds"
            raise ValueError(msg)
        return parsed


class McpConfig(BaseModel):
    """MCP (Model Context Protocol) server configuration.

    Controls the MCP server that exposes articles and search
    to external AI agents like OpenClaw.
    """

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    enabled: bool = Field(
        default=False,
        validation_alias="MCP_ENABLED",
        description="Enable the MCP server for external agent access",
    )
    transport: str = Field(
        default="stdio",
        validation_alias="MCP_TRANSPORT",
        description="Transport protocol: 'stdio' or 'sse'",
    )
    host: str = Field(
        default="127.0.0.1",
        validation_alias="MCP_HOST",
        description="Bind address for SSE transport",
    )
    port: int = Field(
        default=8200,
        validation_alias="MCP_PORT",
        description="Port for SSE transport",
    )
    user_id: int | None = Field(
        default=None,
        validation_alias="MCP_USER_ID",
        description="Optional user ID scope for MCP queries",
        json_schema_extra=SECRET_MARKER,
    )
    allow_remote_sse: bool = Field(
        default=False,
        validation_alias="MCP_ALLOW_REMOTE_SSE",
        description="Allow SSE transport to bind non-loopback hosts",
    )
    allow_unscoped_sse: bool = Field(
        default=False,
        validation_alias="MCP_ALLOW_UNSCOPED_SSE",
        description="Allow SSE transport without MCP_USER_ID scoping",
    )
    allow_unscoped_production: bool = Field(
        default=False,
        validation_alias="MCP_ALLOW_UNSCOPED_PRODUCTION",
        description=(
            "Allow intentionally unscoped MCP SSE in production after "
            "MCP_ALLOW_UNSCOPED_SSE has also been set"
        ),
    )
    allow_unscoped_stdio: bool = Field(
        default=False,
        validation_alias="MCP_ALLOW_UNSCOPED_STDIO",
        description="Allow stdio transport without MCP_USER_ID scoping",
    )
    auth_mode: str = Field(
        default="disabled",
        validation_alias="MCP_AUTH_MODE",
        description="Hosted MCP auth mode: 'disabled' or 'jwt'",
    )
    forwarded_access_token_header: str = Field(
        default="X-Ratatoskr-Forwarded-Access-Token",
        validation_alias="MCP_FORWARDED_ACCESS_TOKEN_HEADER",
        description="Header used by a trusted gateway to forward the original bearer token",
    )
    forwarded_secret_header: str = Field(
        default="X-Ratatoskr-MCP-Forwarding-Secret",
        validation_alias="MCP_FORWARDED_SECRET_HEADER",
        description="Header carrying the shared secret for trusted token forwarding",
    )
    forwarding_secret: str | None = Field(
        default=None,
        validation_alias="MCP_FORWARDING_SECRET",
        description="Optional shared secret required when trusting forwarded bearer tokens",
        json_schema_extra=SECRET_MARKER,
    )

    @field_validator("transport", mode="before")
    @classmethod
    def _validate_transport(cls, value: Any) -> str:
        if value in (None, ""):
            return "stdio"
        normalized: str = str(value).strip().lower()
        if normalized not in ("stdio", "sse"):
            msg = "MCP transport must be 'stdio' or 'sse'"
            raise ValueError(msg)
        return normalized

    @field_validator("auth_mode", mode="before")
    @classmethod
    def _validate_auth_mode(cls, value: Any) -> str:
        if value in (None, ""):
            return "disabled"
        normalized = str(value).strip().lower()
        if normalized not in ("disabled", "jwt"):
            msg = "MCP auth mode must be 'disabled' or 'jwt'"
            raise ValueError(msg)
        return normalized

    @field_validator(
        "forwarded_access_token_header",
        "forwarded_secret_header",
        mode="before",
    )
    @classmethod
    def _validate_header_name(cls, value: Any) -> str:
        if value in (None, ""):
            msg = "MCP forwarded header name must not be empty"
            raise ValueError(msg)
        header_name = str(value).strip()
        if any(ch.isspace() for ch in header_name):
            msg = "MCP forwarded header name must not contain whitespace"
            raise ValueError(msg)
        return header_name

    @field_validator("forwarding_secret", mode="before")
    @classmethod
    def _validate_forwarding_secret(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        secret = str(value).strip()
        return secret or None

    @field_validator("port", mode="before")
    @classmethod
    def _validate_port(cls, value: Any) -> int:
        if value in (None, ""):
            return 8200
        try:
            parsed = int(str(value))
        except ValueError as exc:
            msg = "MCP port must be a valid integer"
            raise ValueError(msg) from exc
        if parsed < 1 or parsed > 65535:
            msg = "MCP port must be between 1 and 65535"
            raise ValueError(msg)
        return parsed

    @field_validator("user_id", mode="before")
    @classmethod
    def _validate_user_id(cls, value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            parsed = int(str(value))
        except ValueError as exc:
            msg = "MCP user ID must be a valid integer"
            raise ValueError(msg) from exc
        if parsed <= 0:
            msg = "MCP user ID must be a positive integer"
            raise ValueError(msg)
        return parsed


class BatchAnalysisConfig(BaseModel):
    """Batch article relationship detection and combined summary configuration."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    enabled: bool = Field(
        default=True,
        validation_alias="BATCH_ANALYSIS_ENABLED",
        description="Enable batch relationship analysis for multi-URL submissions",
    )
    min_articles: int = Field(
        default=2,
        validation_alias="BATCH_ANALYSIS_MIN_ARTICLES",
        description="Minimum successful articles required to trigger analysis",
    )
    series_threshold: float = Field(
        default=0.9,
        validation_alias="BATCH_ANALYSIS_SERIES_THRESHOLD",
        description="Confidence threshold for series detection (0.0-1.0)",
    )
    cluster_threshold: float = Field(
        default=0.75,
        validation_alias="BATCH_ANALYSIS_CLUSTER_THRESHOLD",
        description="Confidence threshold for topic cluster detection (0.0-1.0)",
    )
    combined_summary_enabled: bool = Field(
        default=True,
        validation_alias="BATCH_COMBINED_SUMMARY_ENABLED",
        description="Generate combined summary when relationship is detected",
    )
    use_llm_for_analysis: bool = Field(
        default=True,
        validation_alias="BATCH_ANALYSIS_USE_LLM",
        description="Use LLM for ambiguous relationship analysis",
    )

    @field_validator("min_articles", mode="before")
    @classmethod
    def _validate_min_articles(cls, value: Any) -> int:
        if value in (None, ""):
            return 2
        try:
            parsed = int(str(value))
        except ValueError as exc:
            msg = "Min articles must be a valid integer"
            raise ValueError(msg) from exc
        if parsed < 2 or parsed > 100:
            msg = "Min articles must be between 2 and 100"
            raise ValueError(msg)
        return parsed

    @field_validator("series_threshold", "cluster_threshold", mode="before")
    @classmethod
    def _validate_threshold(cls, value: Any, info: ValidationInfo) -> float:
        default = 0.9 if "series" in info.field_name else 0.75
        if value in (None, ""):
            return default
        try:
            parsed = float(str(value))
        except ValueError as exc:
            msg = f"{info.field_name} must be a valid number"
            raise ValueError(msg) from exc
        if parsed < 0.0 or parsed > 1.0:
            msg = f"{info.field_name} must be between 0.0 and 1.0"
            raise ValueError(msg)
        return parsed


class EmbeddingConfig(BaseModel):
    """Embedding provider configuration."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    provider: str = Field(default="local", validation_alias="EMBEDDING_PROVIDER")
    gemini_api_key: str = Field(
        default="", validation_alias="GEMINI_API_KEY", json_schema_extra=SECRET_MARKER
    )
    gemini_model: str = Field(
        default="gemini-embedding-2-preview",
        validation_alias="GEMINI_EMBEDDING_MODEL",
    )
    gemini_dimensions: int = Field(default=768, validation_alias="GEMINI_EMBEDDING_DIMENSIONS")
    max_token_length: int = Field(default=512, validation_alias="EMBEDDING_MAX_TOKEN_LENGTH")

    @property
    def embedding_dim(self) -> int:
        """Return vector dimension for the configured embedding provider."""
        if self.provider == "gemini":
            return self.gemini_dimensions
        return 384  # all local sentence-transformers models produce 384-dim vectors

    @field_validator("provider", mode="before")
    @classmethod
    def _validate_provider(cls, value: Any) -> str:
        if value in (None, ""):
            return "local"
        normalized: str = str(value).strip().lower()
        if normalized not in ("local", "gemini"):
            msg = "EMBEDDING_PROVIDER must be 'local' or 'gemini'"
            raise ValueError(msg)
        return normalized

    @field_validator("gemini_dimensions", mode="before")
    @classmethod
    def _validate_dimensions(cls, value: Any) -> int:
        if value in (None, ""):
            return 768
        try:
            parsed = int(str(value))
        except ValueError as exc:
            msg = "GEMINI_EMBEDDING_DIMENSIONS must be a valid integer"
            raise ValueError(msg) from exc
        if parsed < 128 or parsed > 3072:
            msg = "GEMINI_EMBEDDING_DIMENSIONS must be between 128 and 3072"
            raise ValueError(msg)
        return parsed

    @field_validator("max_token_length", mode="before")
    @classmethod
    def _validate_max_token_length(cls, value: Any) -> int:
        if value in (None, ""):
            return 512
        try:
            parsed = int(str(value))
        except ValueError as exc:
            msg = "EMBEDDING_MAX_TOKEN_LENGTH must be a valid integer"
            raise ValueError(msg) from exc
        if parsed < 64 or parsed > 8192:
            msg = "EMBEDDING_MAX_TOKEN_LENGTH must be between 64 and 8192"
            raise ValueError(msg)
        return parsed


class QdrantConfig(BaseModel):
    """Vector store configuration for Qdrant."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    url: str = Field(
        default="http://localhost:6333",
        validation_alias="QDRANT_URL",
        description="Qdrant HTTP endpoint (scheme + host + port)",
    )
    api_key: str | None = Field(
        default=None,
        validation_alias="QDRANT_API_KEY",
        description="Optional API key for secured Qdrant deployments",
        json_schema_extra=SECRET_MARKER,
    )
    environment: str = Field(
        default="dev",
        validation_alias=AliasChoices("QDRANT_ENV", "APP_ENV", "ENVIRONMENT"),
        description="Environment label used for namespacing collections",
    )
    user_scope: str = Field(
        default="public",
        validation_alias="QDRANT_USER_SCOPE",
        description="User or tenant scope used for namespacing collections",
    )
    collection_version: str = Field(
        default="v1",
        validation_alias="QDRANT_COLLECTION_VERSION",
        description="Collection version suffix to prevent bleed-over between schema changes",
    )
    required: bool = Field(
        default=False,
        validation_alias="QDRANT_REQUIRED",
        description="If true, fail startup when Qdrant is unavailable. Default false for graceful degradation.",
    )
    connection_timeout: float = Field(
        default=10.0,
        validation_alias="QDRANT_CONNECTION_TIMEOUT",
        description="Connection timeout in seconds for Qdrant HTTP client",
    )

    @field_validator("url", mode="before")
    @classmethod
    def _validate_url(cls, value: Any) -> str:
        url = str(value or "").strip()
        if not url:
            msg = "Qdrant URL must be provided"
            raise ValueError(msg)
        if len(url) > 200:
            msg = "Qdrant URL value appears to be too long"
            raise ValueError(msg)
        if "\x00" in url:
            msg = "Qdrant URL contains invalid characters"
            raise ValueError(msg)
        return url

    @field_validator("api_key", mode="before")
    @classmethod
    def _validate_api_key(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        key = str(value).strip()
        if len(key) > 500:
            msg = "Qdrant API key appears to be too long"
            raise ValueError(msg)
        return key

    @field_validator("environment", "user_scope", mode="before")
    @classmethod
    def _sanitize_names(cls, value: Any, info: ValidationInfo) -> str:
        raw = str(value or "").strip() or cls.model_fields[info.field_name].default
        cleaned = "".join(ch for ch in raw if ch.isalnum() or ch in {"-", "_"})
        if not cleaned:
            msg = f"{info.field_name.replace('_', ' ').capitalize()} cannot be empty"
            raise ValueError(msg)
        return cleaned.lower()

    @field_validator("collection_version", mode="before")
    @classmethod
    def _sanitize_version(cls, value: Any) -> str:
        raw = str(value or "").strip() or "v1"
        cleaned = "".join(ch for ch in raw if ch.isalnum() or ch in {"-", "_"})
        if not cleaned:
            msg = "Collection version cannot be empty"
            raise ValueError(msg)
        return cleaned.lower()


class VectorReconcileConfig(BaseModel):
    """Steady-state vector-index reconciler configuration.

    This Taskiq job is the convergence/backfill path: it periodically scans
    ``summary_embeddings`` for rows whose ``last_indexed_at`` lags
    ``summaries.updated_at`` and re-embeds them. It is not a fallback for
    anything -- the fast-path writer in the persist node handles freshness,
    and this reconciler ensures steady-state convergence.
    """

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    enabled: bool = Field(
        default=True,
        validation_alias="VECTOR_RECONCILE_ENABLED",
        description="Enable the periodic vector-index reconciler",
    )
    cron: str = Field(
        default="*/30 * * * *",
        validation_alias="VECTOR_RECONCILE_CRON",
        description="Cron expression governing reconciler runs (UTC)",
    )
    batch_size: int = Field(
        default=100,
        validation_alias="VECTOR_RECONCILE_BATCH_SIZE",
        description="Maximum number of stale summaries to re-embed per run",
    )

    @field_validator("cron", mode="before")
    @classmethod
    def _validate_cron(cls, value: Any) -> str:
        if value in (None, ""):
            return "*/30 * * * *"
        cron = str(value).strip()
        if len(cron.split()) != 5:
            msg = "Vector reconcile cron must be a 5-field expression"
            raise ValueError(msg)
        return cron

    @field_validator("batch_size", mode="before")
    @classmethod
    def _validate_batch_size(cls, value: Any) -> int:
        if value in (None, ""):
            return 100
        try:
            parsed = int(str(value))
        except ValueError as exc:
            msg = "Vector reconcile batch size must be a valid integer"
            raise ValueError(msg) from exc
        if parsed < 1 or parsed > 10_000:
            msg = "Vector reconcile batch size must be between 1 and 10000"
            raise ValueError(msg)
        return parsed
