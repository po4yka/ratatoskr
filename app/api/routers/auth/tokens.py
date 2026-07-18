"""
JWT token creation and validation.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Any

import jwt

from app.api.exceptions import (
    AuthorizationError,
    ValidationError,
)
from app.config import Config, load_config
from app.core.logging_utils import get_logger
from app.core.time_utils import UTC

logger = get_logger(__name__)

_CLIENT_TYPE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("cli", "cli"),
    ("mcp", "mcp"),
    ("automation", "automation"),
    ("web", "web"),
    ("mobile", "mobile"),
    ("android", "mobile"),
    ("ios", "mobile"),
    ("admin", "admin"),
    ("test", "test"),
)
_SELF_SERVICE_SECRET_CLIENT_TYPES = frozenset({"cli", "mcp", "automation"})


def _load_secret_key() -> str:
    """Load the JWT signing/verification key from the validated AppConfig.

    Reads the canonical ``runtime.jwt_secret_key`` field, which resolves the
    documented ``JWT_SECRET`` / ``JWT_SECRET_KEY`` aliases and applies the length
    floor at config load. Reading the raw env var directly (the previous
    behavior) bypassed that validation and silently ignored the ``JWT_SECRET``
    alias, so an operator who set only ``JWT_SECRET`` got a "must be configured"
    error and broken auth.
    """
    secret = (load_config(allow_stub_telegram=True).runtime.jwt_secret_key or "").strip()

    if not secret or secret == "your-secret-key-change-in-production":
        raise RuntimeError(
            "JWT_SECRET_KEY (or its JWT_SECRET alias) must be set to a secure random value. "
            "Generate one with: openssl rand -hex 32"
        )

    # The field validator already enforces the >=32 floor when the secret is set;
    # this is a defensive backstop and a clearer error at the point of use.
    if len(secret) < 32:
        raise RuntimeError(
            f"JWT_SECRET_KEY must be at least 32 characters long. Current length: {len(secret)}"
        )
    return secret


def _load_previous_secret_keys() -> tuple[str, ...]:
    """Load previous JWT signing keys accepted only for decode during rotation."""
    try:
        raw_previous = Config.get("JWT_SECRET_PREVIOUS_KEYS", "")
    except ValueError:
        raw_previous = ""

    previous: list[str] = []
    for index, part in enumerate(str(raw_previous or "").split(",")):
        secret = part.strip()
        if not secret:
            continue
        if len(secret) < 32:
            raise RuntimeError(
                f"JWT_SECRET_PREVIOUS_KEYS[{index}] must be at least 32 characters long. "
                f"Current length: {len(secret)}"
            )
        previous.append(secret)
    return tuple(previous)


# JWT configuration. Holders wrap mutable lazy-init / one-shot
# warning state so call sites don't need the `global` keyword.
_secret_key_holder: list[str | None] = [None]
_previous_secret_keys_holder: list[tuple[str, ...] | None] = [None]
ALGORITHM = "HS256"
JWT_ISSUER = "ratatoskr"
JWT_AUDIENCE = "ratatoskr-api"
JWT_REQUIRED_CLAIMS = ("exp", "iat", "type", "user_id", "aud", "iss")
JWT_LEGACY_CLAIMS_GRACE_SECONDS = 5 * 60
_JWT_LEGACY_CLAIMS = frozenset({"aud", "iss"})
_jwt_legacy_claim_grace_started_at_holder: list[datetime] = [datetime.now(UTC)]
_allowlist_empty_warned_holder: list[bool] = [False]
ACCESS_TOKEN_EXPIRE_MINUTES = 30
REFRESH_TOKEN_EXPIRE_DAYS = 30


def _get_secret_key() -> str:
    """Return the JWT secret key, loading it lazily on first call."""
    if _secret_key_holder[0] is None:
        _secret_key_holder[0] = _load_secret_key()
        logger.info("JWT authentication initialized")
    return _secret_key_holder[0]


def _get_previous_secret_keys() -> tuple[str, ...]:
    """Return previous JWT keys accepted for decode during a rotation window."""
    if _previous_secret_keys_holder[0] is None:
        _previous_secret_keys_holder[0] = _load_previous_secret_keys()
        if _previous_secret_keys_holder[0]:
            logger.warning(
                "jwt_previous_keys_enabled",
                extra={"previous_key_count": len(_previous_secret_keys_holder[0])},
            )
    return _previous_secret_keys_holder[0]


def create_token(
    user_id: int,
    token_type: str,
    username: str | None = None,
    client_id: str | None = None,
    *,
    ttl_seconds: float | None = None,
) -> str:
    """
    Create JWT token (access or refresh).

    Args:
        user_id: User ID to encode in token
        token_type: "access" or "refresh"
        username: Optional username to include
        client_id: Optional client application ID to include
        ttl_seconds: Override the default TTL (access=30 min, refresh=30 days).

    Returns:
        Encoded JWT token
    """
    now = datetime.now(UTC)
    if token_type == "access":
        if ttl_seconds is None:
            ttl_seconds = ACCESS_TOKEN_EXPIRE_MINUTES * 60
        payload = {
            "user_id": user_id,
            "username": username,
            "client_id": client_id,
            "exp": now + timedelta(seconds=ttl_seconds),
            "type": "access",
            "iat": now,
            "iss": JWT_ISSUER,
            "aud": JWT_AUDIENCE,
            "jti": secrets.token_urlsafe(16),
        }
    elif token_type == "refresh":
        if ttl_seconds is None:
            ttl_seconds = REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60
        payload = {
            "user_id": user_id,
            "client_id": client_id,
            "exp": now + timedelta(seconds=ttl_seconds),
            "type": "refresh",
            "iat": now,
            "iss": JWT_ISSUER,
            "aud": JWT_AUDIENCE,
            "jti": secrets.token_urlsafe(16),
        }
    else:
        raise ValueError(f"Invalid token type: {token_type}")

    return jwt.encode(payload, _get_secret_key(), algorithm=ALGORITHM)


def create_access_token(
    user_id: int, username: str | None = None, client_id: str | None = None
) -> str:
    """Create JWT access token."""
    return create_token(user_id, "access", username, client_id)


async def create_refresh_token(
    user_id: int,
    client_id: str | None = None,
    device_info: str | None = None,
    ip_address: str | None = None,
    auth_repo: Any | None = None,
    *,
    ttl_seconds: float | None = None,
    remember_me: bool = True,
    parent_family_id: str | None = None,
    parent_token_hash: str | None = None,
) -> tuple[str, int]:
    """Create and persist JWT refresh token.

    Args:
        user_id: Telegram user ID.
        client_id: Client application identifier.
        device_info: Device information string.
        ip_address: Client IP address.
        auth_repo: Optional auth repository with cache. If None, creates one.
        ttl_seconds: Override the refresh-token TTL (default REFRESH_TOKEN_EXPIRE_DAYS days).
            Credentials login uses this to issue 12h-or-30d tokens depending on Remember Me.
        remember_me: Tag persisted on the refresh-token row so /refresh rotation
            preserves the TTL family. Defaults to True for Telegram/secret-login
            callers (existing 30-day behavior unchanged).
        parent_family_id: When rotating an existing token, the predecessor's
            ``family_id`` so the new token inherits the family. ``None`` for
            first-login tokens — a fresh UUID4 family is generated.
        parent_token_hash: sha256 of the rotated-out token. ``None`` for the
            root of a family. Stored on the row so the policy can walk the
            chain on the next refresh.

    Returns:
        Tuple of (token_string, session_id) where session_id is the refresh token record ID.
    """
    if ttl_seconds is None:
        ttl_seconds = REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60
    token = create_token(user_id, "refresh", client_id=client_id, ttl_seconds=ttl_seconds)

    # Persist token hash
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)

    if auth_repo is None:
        from app.api.routers.auth.dependencies import get_auth_repository

        auth_repo = get_auth_repository()

    # Family ID: inherit from rotation parent, otherwise root of a new family.
    family_id = parent_family_id if parent_family_id is not None else str(uuid.uuid4())

    session_id = await auth_repo.async_create_refresh_token(
        user_id=user_id,
        token_hash=token_hash,
        client_id=client_id,
        device_info=device_info,
        ip_address=ip_address,
        expires_at=expires_at,
        remember_me=remember_me,
        family_id=family_id,
        parent_token_hash=parent_token_hash,
    )

    return token, session_id


def _legacy_claim_grace_remaining_seconds() -> float:
    expires_at = _jwt_legacy_claim_grace_started_at_holder[0] + timedelta(
        seconds=JWT_LEGACY_CLAIMS_GRACE_SECONDS
    )
    return (expires_at - datetime.now(UTC)).total_seconds()


def _is_legacy_claim_grace_active() -> bool:
    return _legacy_claim_grace_remaining_seconds() > 0


def _payload_audience_matches(value: Any) -> bool:
    if isinstance(value, str):
        return value == JWT_AUDIENCE
    if isinstance(value, list):
        return JWT_AUDIENCE in value
    return False


def _decode_legacy_missing_aud_iss(token: str, secret: str) -> dict[str, Any]:
    payload = jwt.decode(
        token,
        secret,
        algorithms=[ALGORITHM],
        options={
            "require": ["exp", "iat", "type", "user_id"],
            "verify_aud": False,
            "verify_iss": False,
        },
    )
    if "aud" in payload and not _payload_audience_matches(payload["aud"]):
        raise jwt.InvalidAudienceError("Audience doesn't match")
    if "iss" in payload and payload["iss"] != JWT_ISSUER:
        raise jwt.InvalidIssuerError("Invalid issuer")

    missing_claims = sorted(_JWT_LEGACY_CLAIMS.difference(payload))
    logger.warning(
        "jwt_legacy_missing_aud_iss_accepted",
        extra={
            "missing_claims": missing_claims,
            "grace_seconds_remaining": max(0, int(_legacy_claim_grace_remaining_seconds())),
            "removal": (
                "Remove JWT legacy aud/iss grace after one release once old "
                "mobile/web tokens have expired."
            ),
        },
    )
    return payload


def _decode_token_with_contract(token: str, secret: str) -> dict[str, Any]:
    try:
        return jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],
            audience=JWT_AUDIENCE,
            issuer=JWT_ISSUER,
            options={"require": list(JWT_REQUIRED_CLAIMS)},
        )
    except jwt.MissingRequiredClaimError as err:
        if err.claim not in _JWT_LEGACY_CLAIMS or not _is_legacy_claim_grace_active():
            raise
        return _decode_legacy_missing_aud_iss(token, secret)


def decode_token(token: str, expected_type: str | None = None) -> dict[str, Any]:
    """Decode and validate JWT token.

    Args:
        token: The JWT token string
        expected_type: If provided, validates token type matches (access/refresh)

    Raises:
        TokenExpiredError: Token has expired (401)
        TokenInvalidError: Token is malformed or signature invalid (401)
        TokenWrongTypeError: Token type doesn't match expected (401)
    """
    from app.api.exceptions import TokenExpiredError, TokenInvalidError, TokenWrongTypeError

    last_invalid_error: jwt.InvalidTokenError | None = None
    for secret in (_get_secret_key(), *_get_previous_secret_keys()):
        try:
            payload = _decode_token_with_contract(token, secret)
            break
        except jwt.ExpiredSignatureError:
            token_type = expected_type or "access"
            raise TokenExpiredError(token_type) from None
        except jwt.InvalidTokenError as err:
            last_invalid_error = err
    else:
        raise TokenInvalidError(str(last_invalid_error)) from last_invalid_error

    if expected_type and payload.get("type") != expected_type:
        raise TokenWrongTypeError(expected=expected_type, received=payload.get("type", "unknown"))

    return payload


def validate_client_id(client_id: str | None) -> None:
    """Validate client_id against allowlist.

    Raises:
        ValidationError: If client_id is missing or invalid format.
        AuthorizationError: If client_id is not in allowlist.
    """
    if not client_id:
        raise ValidationError(
            "Client ID is required. Please update your app to the latest version.",
            details={"field": "client_id"},
        )

    # Validate format
    if not all(c.isalnum() or c in "-_." for c in client_id):
        logger.warning(
            f"Invalid client ID format: {client_id}",
            extra={"client_id": client_id},
        )
        raise ValidationError("Invalid client ID format.", details={"field": "client_id"})

    if len(client_id) > 100:
        logger.warning(
            f"Client ID too long: {client_id}",
            extra={"client_id": client_id, "length": len(client_id)},
        )
        raise ValidationError("Invalid client ID format.", details={"field": "client_id"})

    # Check against allowlist
    allowed_client_ids = Config.get_allowed_client_ids()

    # If the allowlist is empty, Settings has already rejected production /
    # public deployments unless AUTH_ALLOW_ANY_CLIENT_ID=true was explicit.
    if not allowed_client_ids:
        if not _allowlist_empty_warned_holder[0]:
            override = Config.allow_any_client_id()
            logger.warning(
                "ALLOWED_CLIENT_IDS is empty -- all client IDs are accepted. "
                "Set ALLOWED_CLIENT_IDS to restrict access.",
                extra={"auth_allow_any_client_id": override},
            )
            _allowlist_empty_warned_holder[0] = True
        return

    # Otherwise, client must be in allowlist
    if client_id not in allowed_client_ids:
        logger.warning(
            f"Client ID not in allowlist: {client_id}",
            extra={"client_id": client_id, "allowed_ids": list(allowed_client_ids)},
        )
        raise AuthorizationError("Client application not authorized. Please contact administrator.")

    return


def build_auth_posture_summary(cfg: Any, *, cors_origins_count: int) -> dict[str, Any]:
    """Build a redacted auth startup posture summary."""
    from app.api.routers.auth.cookies import REFRESH_COOKIE_NAME

    allowed_user_count = len(cfg.telegram.allowed_user_ids)
    allowed_client_count = len(cfg.auth.allowed_client_ids)
    return {
        "allowed_user_ids_configured": allowed_user_count > 0,
        "allowed_user_ids_count": allowed_user_count,
        "allowed_client_ids_configured": allowed_client_count > 0,
        "allowed_client_ids_count": allowed_client_count,
        "auth_allow_any_client_id": cfg.auth.allow_any_client_id,
        "refresh_cookie_mode": {
            "name_configured": bool(REFRESH_COOKIE_NAME),
            "httponly": True,
            "secure": True,
            "samesite": "strict",
            "path": "/v1/auth",
        },
        "access_token_ttl_seconds": ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        "refresh_token_ttl_seconds": REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60,
        "cors_origins_count": cors_origins_count,
    }


def log_auth_posture_summary(cfg: Any, *, cors_origins_count: int) -> None:
    """Log redacted auth posture at startup."""
    logger.info(
        "auth_posture_summary",
        extra=build_auth_posture_summary(cfg, cors_origins_count=cors_origins_count),
    )


def resolve_client_type(client_id: str | None) -> str:
    """Return a coarse client type inferred from the client ID."""
    if not client_id:
        return "unknown"

    normalized = client_id.lower()
    if normalized == "webapp":
        return "web"

    if normalized.count(".") >= 2 and all(
        part.isidentifier() or part.isalnum() for part in normalized.split(".")
    ):
        return "mobile"

    for prefix, client_type in _CLIENT_TYPE_PREFIXES:
        if normalized == prefix:
            return client_type
        if any(normalized.startswith(f"{prefix}{separator}") for separator in ("-", "_", ".")):
            return client_type

    return "unknown"


def is_web_client(client_id: str | None) -> bool:
    """Return True when the client expects cookie-only refresh token delivery."""
    return resolve_client_type(client_id) == "web"


def is_self_service_secret_client(client_id: str | None) -> bool:
    """Return whether a client ID is eligible for self-service secret management."""
    return resolve_client_type(client_id) in _SELF_SERVICE_SECRET_CLIENT_TYPES


def supported_self_service_secret_client_types() -> tuple[str, ...]:
    """Return the supported self-service secret client types."""
    return tuple(sorted(_SELF_SERVICE_SECRET_CLIENT_TYPES))
