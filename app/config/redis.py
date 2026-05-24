from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from ._secret_marker import SECRET_MARKER


class RedisConfig(BaseModel):
    """Shared Redis connection settings."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    enabled: bool = Field(default=True, validation_alias="REDIS_ENABLED")
    cache_enabled: bool = Field(default=True, validation_alias="REDIS_CACHE_ENABLED")
    required: bool = Field(
        default=False,
        validation_alias="REDIS_REQUIRED",
        description="If true, fail requests when Redis is unavailable.",
    )
    url: str | None = Field(default=None, validation_alias="REDIS_URL")
    host: str = Field(default="127.0.0.1", validation_alias="REDIS_HOST")
    port: int = Field(default=6379, validation_alias="REDIS_PORT")
    db: int = Field(default=0, validation_alias="REDIS_DB")
    password: str | None = Field(
        default=None, validation_alias="REDIS_PASSWORD", json_schema_extra=SECRET_MARKER
    )
    prefix: str = Field(default="ratatoskr", validation_alias="REDIS_PREFIX")
    socket_timeout: float = Field(default=5.0, validation_alias="REDIS_SOCKET_TIMEOUT")
    cache_timeout_sec: float = Field(default=0.3, validation_alias="REDIS_CACHE_TIMEOUT_SEC")
    reconnect_interval: float = Field(
        default=60.0,
        validation_alias="REDIS_RECONNECT_INTERVAL_SEC",
        description="Interval between reconnection attempts when Redis is unavailable. Set to 0 to disable.",
    )
    firecrawl_ttl_seconds: int = Field(
        default=21_600, validation_alias="REDIS_FIRECRAWL_TTL_SECONDS"
    )
    llm_ttl_seconds: int = Field(default=7_200, validation_alias="REDIS_LLM_TTL_SECONDS")

    # Phase 1: Quick wins cache TTLs
    query_cache_ttl_seconds: int = Field(
        default=300,
        validation_alias="REDIS_QUERY_CACHE_TTL_SECONDS",
        description="TTL for query result cache (default: 5 minutes)",
    )
    trending_cache_ttl_seconds: int = Field(
        default=300,
        validation_alias="REDIS_TRENDING_CACHE_TTL_SECONDS",
        description="TTL for trending topics cache (default: 5 minutes)",
    )
    auth_token_cache_ttl_seconds: int = Field(
        default=604_800,
        validation_alias="REDIS_AUTH_TOKEN_CACHE_TTL_SECONDS",
        description="TTL for auth token cache (default: 7 days)",
    )

    # Phase 2: Performance improvements cache TTLs
    batch_progress_ttl_seconds: int = Field(
        default=3_600,
        validation_alias="REDIS_BATCH_PROGRESS_TTL_SECONDS",
        description="TTL for batch processing progress (default: 1 hour)",
    )
    embedding_cache_ttl_seconds: int = Field(
        default=86_400,
        validation_alias="REDIS_EMBEDDING_CACHE_TTL_SECONDS",
        description="TTL for embedding results cache (default: 24 hours)",
    )

    @field_validator("url", mode="before")
    @classmethod
    def _normalize_url(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        cleaned = str(value).strip()
        if cleaned and len(cleaned) > 200:
            msg = "Redis URL appears too long"
            raise ValueError(msg)
        return cleaned

    @field_validator("host", mode="before")
    @classmethod
    def _validate_host(cls, value: Any) -> str:
        host = str(value or "").strip()
        if not host:
            msg = "Redis host is required when URL is not provided"
            raise ValueError(msg)
        if len(host) > 200:
            msg = "Redis host appears too long"
            raise ValueError(msg)
        return host

    @field_validator("port", "db", mode="before")
    @classmethod
    def _validate_int_bounds(cls, value: Any, info: ValidationInfo) -> int:
        default = cls.model_fields[info.field_name].default
        try:
            parsed = int(str(value if value not in (None, "") else default))
        except ValueError as exc:  # pragma: no cover - defensive
            msg = f"{info.field_name.replace('_', ' ')} must be a valid integer"
            raise ValueError(msg) from exc
        limits: dict[str, tuple[int, int]] = {
            "port": (0, 65535),
            "db": (0, 65535),
        }
        min_val, max_val = limits.get(info.field_name, (0, 65535))
        if parsed < min_val or parsed > max_val:
            msg = (
                f"{info.field_name.replace('_', ' ').capitalize()} must be between "
                f"{min_val} and {max_val}"
            )
            raise ValueError(msg)
        return parsed

    @field_validator("socket_timeout", mode="before")
    @classmethod
    def _validate_timeout(cls, value: Any) -> float:
        default = cls.model_fields["socket_timeout"].default
        try:
            parsed = float(str(value if value not in (None, "") else default))
        except ValueError as exc:  # pragma: no cover - defensive
            msg = "Redis socket timeout must be a valid number"
            raise ValueError(msg) from exc
        if parsed <= 0 or parsed > 60:
            msg = "Redis socket timeout must be between 0 and 60 seconds"
            raise ValueError(msg)
        return parsed

    @field_validator("cache_timeout_sec", mode="before")
    @classmethod
    def _validate_cache_timeout(cls, value: Any) -> float:
        default = cls.model_fields["cache_timeout_sec"].default
        try:
            parsed = float(str(value if value not in (None, "") else default))
        except ValueError as exc:  # pragma: no cover - defensive
            msg = "Redis cache timeout must be a valid number"
            raise ValueError(msg) from exc
        if parsed <= 0 or parsed > 5:
            msg = "Redis cache timeout must be between 0 and 5 seconds"
            raise ValueError(msg)
        return parsed

    @field_validator("reconnect_interval", mode="before")
    @classmethod
    def _validate_reconnect_interval(cls, value: Any) -> float:
        default = cls.model_fields["reconnect_interval"].default
        try:
            parsed = float(str(value if value not in (None, "") else default))
        except ValueError as exc:  # pragma: no cover - defensive
            msg = "Redis reconnect interval must be a valid number"
            raise ValueError(msg) from exc
        if parsed < 0 or parsed > 300:
            msg = "Redis reconnect interval must be between 0 and 300 seconds"
            raise ValueError(msg)
        return parsed

    @field_validator("prefix", mode="before")
    @classmethod
    def _validate_prefix(cls, value: Any) -> str:
        prefix = str(value or "ratatoskr").strip()
        if not prefix:
            msg = "Redis prefix cannot be empty"
            raise ValueError(msg)
        if len(prefix) > 50:
            msg = "Redis prefix appears too long"
            raise ValueError(msg)
        if any(ch in prefix for ch in (" ", "\t", "\n", "\r")):
            msg = "Redis prefix cannot contain whitespace"
            raise ValueError(msg)
        return prefix

    @field_validator(
        "firecrawl_ttl_seconds",
        "llm_ttl_seconds",
        "query_cache_ttl_seconds",
        "trending_cache_ttl_seconds",
        "auth_token_cache_ttl_seconds",
        "batch_progress_ttl_seconds",
        "embedding_cache_ttl_seconds",
        mode="before",
    )
    @classmethod
    def _validate_ttl_seconds(cls, value: Any, info: ValidationInfo) -> int:
        """Validate TTL fields with field-specific bounds."""
        default = cls.model_fields[info.field_name].default
        try:
            parsed = int(str(value if value not in (None, "") else default))
        except ValueError as exc:  # pragma: no cover - defensive
            msg = f"{info.field_name.replace('_', ' ')} must be a valid integer"
            raise ValueError(msg) from exc

        # Field-specific bounds: (min_seconds, max_seconds)
        ttl_limits: dict[str, tuple[int, int]] = {
            "firecrawl_ttl_seconds": (60, 86_400 * 14),  # 1 min to 14 days
            "llm_ttl_seconds": (60, 86_400 * 14),  # 1 min to 14 days
            "query_cache_ttl_seconds": (10, 86_400),  # 10 sec to 1 day
            "trending_cache_ttl_seconds": (10, 86_400),  # 10 sec to 1 day
            "auth_token_cache_ttl_seconds": (60, 86_400 * 30),  # 1 min to 30 days
            "batch_progress_ttl_seconds": (60, 86_400),  # 1 min to 1 day
            "embedding_cache_ttl_seconds": (300, 86_400 * 7),  # 5 min to 7 days
        }
        min_val, max_val = ttl_limits.get(info.field_name, (60, 86_400 * 14))

        if parsed < min_val or parsed > max_val:
            msg = (
                f"{info.field_name.replace('_', ' ').capitalize()} must be between "
                f"{min_val} and {max_val} seconds"
            )
            raise ValueError(msg)
        return parsed
