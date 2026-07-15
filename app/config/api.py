from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from app.config._secret_marker import SECRET_MARKER
from app.core.logging_utils import get_logger

logger = get_logger(__name__)


class ApiLimitsConfig(BaseModel):
    """API rate limiting configuration."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    window_seconds: int = Field(default=60, validation_alias="API_RATE_LIMIT_WINDOW_SECONDS")
    cooldown_multiplier: float = Field(
        default=2.0, validation_alias="API_RATE_LIMIT_COOLDOWN_MULTIPLIER"
    )
    max_concurrent: int = Field(
        default=3, validation_alias="API_RATE_LIMIT_MAX_CONCURRENT_PER_USER"
    )
    default_limit: int = Field(default=100, validation_alias="API_RATE_LIMIT_DEFAULT")
    summaries_limit: int = Field(default=200, validation_alias="API_RATE_LIMIT_SUMMARIES")
    requests_limit: int = Field(default=10, validation_alias="API_RATE_LIMIT_REQUESTS")
    search_limit: int = Field(default=50, validation_alias="API_RATE_LIMIT_SEARCH")
    auth_limit: int = Field(default=20, validation_alias="API_RATE_LIMIT_AUTH")
    secret_login_limit: int = Field(
        default=10,
        validation_alias="API_RATE_LIMIT_SECRET_LOGIN",
    )
    credentials_login_limit: int = Field(
        default=5,
        validation_alias="API_RATE_LIMIT_CREDENTIALS_LOGIN",
    )
    aggregation_create_user_limit: int = Field(
        default=5,
        validation_alias="API_RATE_LIMIT_AGGREGATION_CREATE_USER",
    )
    aggregation_create_client_limit: int = Field(
        default=20,
        validation_alias="API_RATE_LIMIT_AGGREGATION_CREATE_CLIENT",
    )

    @field_validator("window_seconds", mode="before")
    @classmethod
    def _validate_window(cls, value: Any) -> int:
        try:
            parsed = int(str(value if value not in (None, "") else 60))
        except ValueError as exc:
            msg = "API rate limit window must be a valid integer"
            raise ValueError(msg) from exc
        if parsed <= 0 or parsed > 3600:
            msg = "API rate limit window must be between 1 and 3600 seconds"
            raise ValueError(msg)
        return parsed

    @field_validator("cooldown_multiplier", mode="before")
    @classmethod
    def _validate_cooldown_multiplier(cls, value: Any) -> float:
        try:
            parsed = float(str(value if value not in (None, "") else 2.0))
        except ValueError as exc:
            msg = "Cooldown multiplier must be a valid number"
            raise ValueError(msg) from exc
        if parsed < 0 or parsed > 10:
            msg = "Cooldown multiplier must be between 0 and 10"
            raise ValueError(msg)
        return parsed

    @field_validator(
        "max_concurrent",
        "default_limit",
        "summaries_limit",
        "requests_limit",
        "search_limit",
        "auth_limit",
        "secret_login_limit",
        "credentials_login_limit",
        "aggregation_create_user_limit",
        "aggregation_create_client_limit",
        mode="before",
    )
    @classmethod
    def _validate_limits(cls, value: Any, info: ValidationInfo) -> int:
        default = cls.model_fields[info.field_name].default
        try:
            parsed = int(str(value if value not in (None, "") else default))
        except ValueError as exc:
            msg = f"{info.field_name.replace('_', ' ')} must be a valid integer"
            raise ValueError(msg) from exc
        if parsed <= 0 or parsed > 10000:
            msg = f"{info.field_name.replace('_', ' ').capitalize()} must be between 1 and 10000"
            raise ValueError(msg)
        return parsed


class AuthConfig(BaseModel):
    """Authentication feature configuration."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    secret_login_enabled: bool = Field(
        default=False,
        validation_alias="SECRET_LOGIN_ENABLED",
        description="Enable alternate secret-key login flow",
    )
    secret_min_length: int = Field(
        default=32,
        validation_alias="SECRET_LOGIN_MIN_LENGTH",
        description="Minimum length for client-provided secrets",
    )
    secret_max_length: int = Field(
        default=128,
        validation_alias="SECRET_LOGIN_MAX_LENGTH",
        description="Maximum length for client-provided secrets",
    )
    secret_max_failed_attempts: int = Field(
        default=5,
        validation_alias="SECRET_LOGIN_MAX_FAILED_ATTEMPTS",
        description="Maximum failed attempts before lockout",
    )
    secret_lockout_minutes: int = Field(
        default=15,
        validation_alias="SECRET_LOGIN_LOCKOUT_MINUTES",
        description="Lockout duration after repeated failures",
    )
    secret_pepper: str | None = Field(
        default=None,
        validation_alias="SECRET_LOGIN_PEPPER",
        description="Optional pepper used when hashing secret keys",
        json_schema_extra=SECRET_MARKER,
    )

    credentials_pepper: str | None = Field(
        default=None,
        validation_alias="CREDENTIALS_LOGIN_PEPPER",
        description=(
            "Pepper applied as HMAC pre-hash before argon2id. Required to use "
            "credentials login; must be independent of JWT_SECRET_KEY and "
            "SECRET_LOGIN_PEPPER (enforced at config load by "
            "Settings._ensure_auth_secret_domain_separation). When unset, the "
            "credentials-login route returns 503 Configuration error -- the "
            "pepper presence is the only gate."
        ),
        json_schema_extra=SECRET_MARKER,
    )
    credentials_max_failed_attempts: int = Field(
        default=5,
        validation_alias="CREDENTIALS_LOGIN_MAX_FAILED_ATTEMPTS",
        description="Maximum failed credential attempts before lockout",
    )
    credentials_lockout_minutes: int = Field(
        default=15,
        validation_alias="CREDENTIALS_LOGIN_LOCKOUT_MINUTES",
        description="Lockout duration after repeated credential failures",
    )
    credentials_password_min_length: int = Field(
        default=12,
        validation_alias="CREDENTIALS_LOGIN_PASSWORD_MIN_LENGTH",
        description="Minimum password length",
    )
    credentials_password_max_length: int = Field(
        default=256,
        validation_alias="CREDENTIALS_LOGIN_PASSWORD_MAX_LENGTH",
        description="Maximum password length (DoS guard for argon2)",
    )
    credentials_remember_me_days: int = Field(
        default=30,
        validation_alias="CREDENTIALS_LOGIN_REMEMBER_ME_DAYS",
        description="Refresh-token TTL when Remember Me is checked (days)",
    )
    credentials_no_remember_hours: int = Field(
        default=12,
        validation_alias="CREDENTIALS_LOGIN_NO_REMEMBER_HOURS",
        description=(
            "Refresh-token TTL when Remember Me is unchecked (hours). "
            "Frontend stores tokens in sessionStorage in this mode so they "
            "vanish on browser close regardless of TTL."
        ),
    )
    credentials_argon2_time_cost: int = Field(
        default=3,
        validation_alias="CREDENTIALS_LOGIN_ARGON2_TIME_COST",
        description="argon2id iterations (RFC 9106 recommends >=3)",
    )
    credentials_argon2_memory_kib: int = Field(
        default=65536,
        validation_alias="CREDENTIALS_LOGIN_ARGON2_MEMORY_KIB",
        description="argon2id memory cost in KiB (default 64 MiB)",
    )
    credentials_argon2_parallelism: int = Field(
        default=1,
        validation_alias="CREDENTIALS_LOGIN_ARGON2_PARALLELISM",
        description="argon2id parallelism (lanes)",
    )
    argon2_max_concurrency: int = Field(
        default=2,
        validation_alias="AUTH_ARGON2_MAX_CONCURRENCY",
        description=(
            "Maximum concurrent Argon2 operations per API process. Bounds the "
            "memory used by password and client-secret authentication."
        ),
    )
    metrics_bearer_token: str | None = Field(
        default=None,
        validation_alias="METRICS_BEARER_TOKEN",
        description="Bearer token accepted by the internal Prometheus scrape endpoint",
        json_schema_extra=SECRET_MARKER,
    )
    apple_client_id: str | None = Field(
        default=None,
        validation_alias="APPLE_SIGNIN_CLIENT_ID",
        description="Apple Services ID or bundle ID accepted as id_token audience",
    )
    apple_team_id: str | None = Field(
        default=None,
        validation_alias="APPLE_SIGNIN_TEAM_ID",
        description="Apple developer Team ID used for documentation and client setup checks",
    )
    magic_link_verify_url: str | None = Field(
        default=None,
        validation_alias="MAGIC_LINK_VERIFY_URL",
        description="Public URL used in magic-link emails; API appends token and client_id",
    )

    allowed_client_ids: tuple[str, ...] = Field(
        default_factory=tuple,
        validation_alias="ALLOWED_CLIENT_IDS",
        description=(
            "Comma-separated list of mobile/CLI client_id values allowed to "
            "obtain access tokens (e.g. android-app, ios-app, cli). Empty "
            "tuple = no restriction only for local/dev or when "
            "AUTH_ALLOW_ANY_CLIENT_ID=true is explicitly set. Values are "
            "validated to alphanumeric + - _ . characters, max 100 chars; "
            "invalid pieces are silently dropped with a warning log."
        ),
    )
    allow_any_client_id: bool = Field(
        default=False,
        validation_alias="AUTH_ALLOW_ANY_CLIENT_ID",
        description=(
            "Explicitly allow any syntactically valid client_id when "
            "ALLOWED_CLIENT_IDS is empty. Required to keep fail-open client ID "
            "semantics in production/public deployments."
        ),
    )

    @field_validator("allowed_client_ids", mode="before")
    @classmethod
    def _parse_client_ids(cls, value: Any) -> tuple[str, ...]:
        if value in (None, "", ()):
            return ()
        if isinstance(value, (list, tuple)):
            raw_pieces = [str(piece) for piece in value]
        else:
            raw_pieces = str(value).split(",")
        client_ids: list[str] = []
        for piece in raw_pieces:
            piece = piece.strip()
            if not piece:
                continue
            if not all(c.isalnum() or c in "-_." for c in piece):
                logger.warning(
                    f"Ignoring invalid client ID format: {piece}",
                    extra={"client_id": piece},
                )
                continue
            if len(piece) > 100:
                logger.warning(
                    f"Ignoring client ID that is too long: {piece}",
                    extra={"client_id": piece, "length": len(piece)},
                )
                continue
            client_ids.append(piece)
        return tuple(client_ids)

    @model_validator(mode="after")
    def _validate_lengths(self) -> AuthConfig:
        if self.secret_min_length <= 0 or self.secret_max_length <= 0:
            raise ValueError("secret lengths must be positive")
        if self.secret_min_length >= self.secret_max_length:
            raise ValueError("secret_min_length must be less than secret_max_length")
        if self.credentials_password_min_length >= self.credentials_password_max_length:
            raise ValueError(
                "credentials_password_min_length must be less than credentials_password_max_length"
            )
        # If a pepper is set, enforce the 32-char floor at config load. An
        # unset pepper is allowed -- credentials-login is gated by pepper
        # presence at request time, so deploys that don't use the feature
        # don't have to set it. A short pepper, however, is a misconfiguration
        # we want to catch before any login attempt.
        pepper = self.credentials_pepper
        if pepper and len(pepper) < 32:
            raise ValueError(
                "CREDENTIALS_LOGIN_PEPPER must be at least 32 chars. "
                "Generate one with: openssl rand -hex 32"
            )
        return self

    @field_validator("secret_max_failed_attempts", mode="before")
    @classmethod
    def _validate_failed_attempts(cls, value: Any) -> int:
        default = cls.model_fields["secret_max_failed_attempts"].default
        try:
            parsed = int(str(value if value not in (None, "") else default))
        except ValueError as exc:
            msg = "secret_max_failed_attempts must be a valid integer"
            raise ValueError(msg) from exc
        if parsed <= 0 or parsed > 100:
            msg = "secret_max_failed_attempts must be between 1 and 100"
            raise ValueError(msg)
        return parsed

    @field_validator("secret_lockout_minutes", mode="before")
    @classmethod
    def _validate_lockout_minutes(cls, value: Any) -> int:
        default = cls.model_fields["secret_lockout_minutes"].default
        try:
            parsed = int(str(value if value not in (None, "") else default))
        except ValueError as exc:
            msg = "secret_lockout_minutes must be a valid integer"
            raise ValueError(msg) from exc
        if parsed < 1 or parsed > 24 * 60:
            msg = "secret_lockout_minutes must be between 1 and 1440"
            raise ValueError(msg)
        return parsed

    @field_validator("secret_min_length", "secret_max_length", mode="before")
    @classmethod
    def _validate_lengths_fields(cls, value: Any, info: ValidationInfo) -> int:
        defaults = {"secret_min_length": 32, "secret_max_length": 128}
        try:
            parsed = int(str(value if value not in (None, "") else defaults[info.field_name]))
        except ValueError as exc:
            msg = f"{info.field_name.replace('_', ' ')} must be a valid integer"
            raise ValueError(msg) from exc
        if parsed <= 0 or parsed > 4096:
            msg = f"{info.field_name.replace('_', ' ')} must be between 1 and 4096"
            raise ValueError(msg)
        return parsed

    @field_validator("secret_pepper", mode="before")
    @classmethod
    def _validate_pepper(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        pepper = str(value).strip()
        # 32-char floor matches credentials_pepper and ensures we never accept
        # a JWT-key-derived value at the field level. The previous 16-char
        # warning was too lenient.
        if len(pepper) < 32:
            msg = (
                "SECRET_LOGIN_PEPPER must be at least 32 chars. "
                "Generate one with: openssl rand -hex 32"
            )
            raise ValueError(msg)
        if len(pepper) > 500:
            msg = "SECRET_LOGIN_PEPPER appears too long"
            raise ValueError(msg)
        return pepper

    @field_validator("credentials_pepper", mode="before")
    @classmethod
    def _validate_credentials_pepper(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        pepper = str(value).strip()
        if len(pepper) > 500:
            msg = "CREDENTIALS_LOGIN_PEPPER appears too long"
            raise ValueError(msg)
        return pepper

    @field_validator("metrics_bearer_token", mode="before")
    @classmethod
    def _validate_metrics_bearer_token(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        token = str(value).strip()
        if len(token) < 32 or len(token) > 512:
            raise ValueError("METRICS_BEARER_TOKEN must contain 32 to 512 characters")
        return token

    @field_validator(
        "credentials_max_failed_attempts",
        "credentials_password_min_length",
        "credentials_password_max_length",
        "credentials_remember_me_days",
        "credentials_no_remember_hours",
        "credentials_argon2_time_cost",
        "credentials_argon2_parallelism",
        "credentials_argon2_memory_kib",
        "credentials_lockout_minutes",
        mode="before",
    )
    @classmethod
    def _validate_credentials_ints(cls, value: Any, info: ValidationInfo) -> int:
        default = cls.model_fields[info.field_name].default
        try:
            parsed = int(str(value if value not in (None, "") else default))
        except ValueError as exc:
            msg = f"{info.field_name.replace('_', ' ')} must be a valid integer"
            raise ValueError(msg) from exc
        if parsed <= 0:
            msg = f"{info.field_name.replace('_', ' ')} must be positive"
            raise ValueError(msg)
        return parsed

    @field_validator("argon2_max_concurrency", mode="before")
    @classmethod
    def _validate_argon2_max_concurrency(cls, value: Any) -> int:
        default = cls.model_fields["argon2_max_concurrency"].default
        try:
            parsed = int(str(value if value not in (None, "") else default))
        except ValueError as exc:
            raise ValueError("argon2 max concurrency must be a valid integer") from exc
        if parsed < 1 or parsed > 8:
            raise ValueError("argon2 max concurrency must be between 1 and 8")
        return parsed


class SyncConfig(BaseModel):
    """Mobile sync configuration."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    expiry_hours: int = Field(default=1, validation_alias="SYNC_EXPIRY_HOURS")
    default_limit: int = Field(default=200, validation_alias="SYNC_DEFAULT_LIMIT")
    min_limit: int = Field(default=1, validation_alias="SYNC_MIN_LIMIT")
    max_limit: int = Field(default=500, validation_alias="SYNC_MAX_LIMIT")
    target_payload_kb: int = Field(default=512, validation_alias="SYNC_TARGET_PAYLOAD_KB")

    @field_validator("expiry_hours", mode="before")
    @classmethod
    def _validate_expiry(cls, value: Any) -> int:
        try:
            parsed = int(str(value if value not in (None, "") else 1))
        except ValueError as exc:
            msg = "Sync expiry hours must be a valid integer"
            raise ValueError(msg) from exc
        if parsed <= 0 or parsed > 168:
            msg = "Sync expiry hours must be between 1 and 168"
            raise ValueError(msg)
        return parsed

    @field_validator("default_limit", mode="before")
    @classmethod
    def _validate_default_limit(cls, value: Any) -> int:
        try:
            parsed = int(str(value if value not in (None, "") else 200))
        except ValueError as exc:
            msg = "Sync default limit must be a valid integer"
            raise ValueError(msg) from exc
        if parsed < 1 or parsed > 500:
            msg = "Sync default limit must be between 1 and 500"
            raise ValueError(msg)
        return parsed

    @field_validator("min_limit", "max_limit", mode="before")
    @classmethod
    def _validate_limits(cls, value: Any, info: ValidationInfo) -> int:
        defaults = {"min_limit": 1, "max_limit": 500}
        try:
            parsed = int(str(value if value not in (None, "") else defaults[info.field_name]))
        except ValueError as exc:
            msg = f"{info.field_name.replace('_', ' ')} must be a valid integer"
            raise ValueError(msg) from exc
        if parsed <= 0 or parsed > 1000:
            msg = f"{info.field_name.replace('_', ' ').capitalize()} must be between 1 and 1000"
            raise ValueError(msg)
        return parsed

    @model_validator(mode="after")
    def _validate_ranges(self) -> SyncConfig:
        if self.min_limit > self.max_limit:
            raise ValueError("sync min_limit cannot exceed max_limit")
        if self.default_limit < self.min_limit or self.default_limit > self.max_limit:
            raise ValueError("sync default_limit must be within min_limit and max_limit")
        return self

    @field_validator("target_payload_kb", mode="before")
    @classmethod
    def _validate_target_payload(cls, value: Any) -> int:
        try:
            parsed = int(str(value if value not in (None, "") else 512))
        except ValueError as exc:
            msg = "Sync target payload size must be a valid integer"
            raise ValueError(msg) from exc
        if parsed < 64 or parsed > 4096:
            msg = "Sync target payload size must be between 64 and 4096 KB"
            raise ValueError(msg)
        return parsed
