"""
Secret-key authentication: hashing, validation, and lockout management.
"""

import hashlib
import hmac
import secrets
from datetime import datetime
from typing import Any, cast

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, InvalidHashError

from app.api.dependencies.database import get_auth_repository
from app.api.exceptions import (
    AuthenticationError,
    AuthorizationError,
    FeatureDisabledError,
    ValidationError,
)
from app.api.models.auth import ClientSecretInfo
from app.config import AppConfig, Config, load_config
from app.core.logging_utils import get_logger
from app.core.time_utils import UTC

logger = get_logger(__name__)

# Module-level cached config. Wrapped in a single-element list so the
# lazy-init site does not need the `global` keyword.
_cfg_holder: list[AppConfig | None] = [None]


def _get_cfg() -> AppConfig:
    """Load and cache application configuration."""
    if _cfg_holder[0] is None:
        _cfg_holder[0] = load_config(allow_stub_telegram=True)
    return _cfg_holder[0]


def _get_auth_config() -> Any:
    """Get auth configuration."""
    cfg = _get_cfg()
    return cfg.auth


def _get_secret_pepper() -> str:
    """Return the SECRET_LOGIN_PEPPER used to hash client secrets.

    Two unrelated security domains must not share one secret: rotating
    JWT_SECRET_KEY would invalidate every stored ClientSecret.secret_hash and
    lock every machine client out of secret-login. JWT signing keys also live
    in different places (env, CI runners, deploy secrets) than DB peppers
    should. The previous fallback to jwt_secret_key was removed for that
    reason — see docs/tasks/issues archive: decouple-secret-login-pepper-
    from-jwt-key.

    Raises RuntimeError when secret-login is enabled but the pepper is unset.
    """
    cfg = _get_cfg()
    if cfg.auth.secret_pepper:
        return cfg.auth.secret_pepper
    raise RuntimeError(
        "SECRET_LOGIN_PEPPER is unset but SECRET_LOGIN_ENABLED=true. "
        "Generate one with `openssl rand -hex 32` (≥32 chars required) and "
        "set it independently of JWT_SECRET_KEY. The previous fallback to "
        "the JWT signing key was removed because rotating JWT_SECRET_KEY "
        "would invalidate every stored ClientSecret.secret_hash."
    )


def coerce_naive(dt_value: datetime | None) -> datetime | None:
    """Convert timezone-aware datetime to naive (UTC assumed)."""
    if dt_value is None:
        return None
    if dt_value.tzinfo:
        return dt_value.replace(tzinfo=None)
    return dt_value


def utcnow_naive() -> datetime:
    """Get current UTC time as naive datetime."""
    return datetime.now(UTC).replace(tzinfo=None)


def ensure_secret_login_enabled() -> None:
    """Raise FeatureDisabledError if secret login is disabled."""
    if not _get_auth_config().secret_login_enabled:
        raise FeatureDisabledError("secret-login", "Secret-key login is disabled")


def ensure_user_allowed(user_id: int) -> None:
    """Raise AuthorizationError if user is not in the allowed list."""
    if not Config.is_user_allowed(user_id, fail_open_when_empty=False):
        logger.warning(
            "User not authorized for secret login",
            extra={"user_id": user_id},
        )
        raise AuthorizationError("User not authorized. Contact administrator to request access.")


def validate_secret_value(secret: str, *, context: str = "login") -> str:
    """Validate provided secret length.

    Args:
        secret: The secret string to validate
        context: Either "login" or "create" for error message context

    Returns:
        Cleaned secret value

    Raises:
        AuthenticationError: For login context with invalid length
        ValidationError: For create context with invalid length
    """
    cfg = _get_auth_config()
    cleaned = secret.strip()
    length = len(cleaned)
    if length < cfg.secret_min_length or length > cfg.secret_max_length:
        if context == "login":
            raise AuthenticationError("Invalid secret length")
        raise ValidationError("Invalid secret length", details={"field": "secret"})
    return cleaned


# Argon2id parameters for client-secret hashing. A slow KDF (vs the previous
# fast HMAC-SHA256) makes offline brute force of a leaked client_secrets table
# infeasible even for low-entropy, user-provided secrets (CWE-916).
_password_hasher_holder: list[PasswordHasher | None] = [None]


def _get_password_hasher() -> PasswordHasher:
    if _password_hasher_holder[0] is None:
        _password_hasher_holder[0] = PasswordHasher(
            time_cost=3, memory_cost=65536, parallelism=2
        )
    return _password_hasher_holder[0]


def _peppered_input(secret: str, salt: str) -> str:
    """Combine the per-secret salt + server pepper with the secret.

    The pepper (an env secret) is mixed in via HMAC so a DB-only leak (without
    the pepper) cannot even begin an argon2 brute force.
    """
    pepper = _get_secret_pepper().encode()
    return hmac.new(pepper, f"{salt}:{secret}".encode(), hashlib.sha256).hexdigest()


def hash_secret(secret: str, salt: str) -> str:
    """Hash a client secret with argon2id (peppered, per-secret salt mixed in).

    Returns the standard ``$argon2id$...`` encoded string. Legacy rows store a
    bare HMAC-SHA256 hex digest; :func:`verify_secret` accepts both.
    """
    return _get_password_hasher().hash(_peppered_input(secret, salt))


def _legacy_hmac_hash(secret: str, salt: str) -> str:
    """Pre-migration HMAC-SHA256 hash, kept only for verifying old rows."""
    pepper = _get_secret_pepper().encode()
    return hmac.new(pepper, f"{salt}:{secret}".encode(), hashlib.sha256).hexdigest()


def verify_secret(secret: str, salt: str, stored_hash: str) -> bool:
    """Constant-time verify a client secret against the stored hash.

    Supports both the argon2id format (new) and the legacy HMAC-SHA256 hex
    digest (old rows, until they are rotated). Never raises on mismatch.
    """
    if not stored_hash:
        return False
    if stored_hash.startswith("$argon2"):
        # Catch only argon2 verification failures (mismatch / malformed hash) ->
        # treat as no-match. Config errors (e.g. missing pepper) propagate so a
        # misconfiguration surfaces loudly instead of failing silently.
        try:
            return _get_password_hasher().verify(stored_hash, _peppered_input(secret, salt))
        except (Argon2Error, InvalidHashError):
            return False
    return hmac.compare_digest(_legacy_hmac_hash(secret, salt), stored_hash)


def generate_secret_value() -> str:
    """Generate a secure random secret value."""
    cfg = _get_auth_config()
    target_len = max(cfg.secret_min_length, 32)
    while True:
        candidate = secrets.token_urlsafe(target_len)
        if len(candidate) >= cfg.secret_min_length:
            break
    if len(candidate) > cfg.secret_max_length:
        candidate = candidate[: cfg.secret_max_length]
    return candidate


def serialize_secret(record: dict[str, Any]) -> ClientSecretInfo:
    """Serialize a client secret dict to ClientSecretInfo."""

    def _fmt(dt_value: datetime | str | None) -> str | None:
        if dt_value is None:
            return None
        if isinstance(dt_value, str):
            return dt_value if dt_value.endswith("Z") else dt_value + "Z"
        return dt_value.isoformat() + "Z"

    # Handle user_id - may be nested dict or direct value
    user_id = record.get("user_id")
    if user_id is None and isinstance(record.get("user"), dict):
        user_id = record["user"].get("telegram_user_id")
    elif user_id is None:
        user_id = record.get("user")

    return ClientSecretInfo(
        id=record.get("id", 0),
        user_id=user_id or 0,
        client_id=record.get("client_id", ""),
        status=record.get("status", "unknown"),
        label=record.get("label"),
        description=record.get("description"),
        expires_at=_fmt(record.get("expires_at")),
        last_used_at=_fmt(record.get("last_used_at")),
        failed_attempts=record.get("failed_attempts") or 0,
        locked_until=_fmt(record.get("locked_until")),
        created_at=_fmt(record.get("created_at")) or "",
        updated_at=_fmt(record.get("updated_at")) or "",
    )


async def check_expired(record: dict[str, Any]) -> None:
    """Check if secret has expired and update status if so."""
    now = utcnow_naive()
    expires_at = record.get("expires_at")
    if expires_at:
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00")).replace(
                tzinfo=None
            )
        if expires_at < now:
            auth_repo = get_auth_repository()
            owner_user_id = record.get("user_id") or record.get("user")
            await auth_repo.async_update_client_secret(
                record["id"],
                owner_user_id=int(owner_user_id) if owner_user_id is not None else None,
                status="expired",
            )
            raise AuthenticationError("Secret has expired")


async def handle_failed_attempt(record: dict[str, Any]) -> dict[str, Any]:
    """Increment failed attempts and potentially lock the secret."""
    cfg = _get_auth_config()
    auth_repo = get_auth_repository()
    return cast(
        "dict[str, Any]",
        await auth_repo.async_increment_failed_attempts(
            record["id"],
            max_attempts=cfg.secret_max_failed_attempts,
            lockout_minutes=cfg.secret_lockout_minutes,
        ),
    )


async def reset_failed_attempts(record: dict[str, Any]) -> None:
    """Reset failed attempts and unlock secret."""
    auth_repo = get_auth_repository()
    await auth_repo.async_reset_failed_attempts(record["id"])


async def build_secret_record(
    user_id: int,
    client_id: str,
    *,
    provided_secret: str | None,
    label: str | None,
    description: str | None,
    expires_at: datetime | None,
) -> tuple[str, dict[str, Any]]:
    """Build and create a client secret record.

    Returns:
        Tuple of (secret_value, record_dict).
    """
    secret_value = (
        validate_secret_value(provided_secret, context="create")
        if provided_secret
        else generate_secret_value()
    )
    salt = secrets.token_hex(16)
    secret_hash = hash_secret(secret_value, salt)

    auth_repo = get_auth_repository()
    record_id = await auth_repo.async_replace_active_client_secret(
        user_id=user_id,
        client_id=client_id,
        secret_hash=secret_hash,
        secret_salt=salt,
        status="active",
        label=label,
        description=description,
        expires_at=expires_at,
    )

    # Fetch the created record to return
    record = await auth_repo.async_get_client_secret_by_id(record_id)
    return secret_value, record or {}
