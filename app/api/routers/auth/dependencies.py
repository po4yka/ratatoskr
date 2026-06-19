"""
FastAPI authentication dependencies.
"""

from __future__ import annotations

from typing import Any, TypedDict

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.dependencies.database import get_auth_repository as get_db_auth_repository
from app.api.exceptions import AuthenticationError, AuthorizationError
from app.api.routers.auth.tokens import decode_token, validate_client_id
from app.config import Config
from app.core.logging_utils import get_logger


class AuthenticatedUser(TypedDict):
    user_id: int
    username: str | None
    client_id: str


logger = get_logger(__name__)

# HTTPBearer security scheme for JWT authentication
# auto_error=False so missing Bearer token doesn't 403 before we check WebApp auth
security = HTTPBearer(auto_error=False)

# Cached instances for dependency injection. Holders wrap mutable
# lazy-init state so the call site does not need the `global` keyword.
_auth_token_cache_holder: list[Any] = [None]
_redis_cache_holder: list[Any] = [None]


def _get_auth_token_cache() -> Any:
    """Get or create the auth token cache singleton."""
    if _auth_token_cache_holder[0] is not None:
        return _auth_token_cache_holder[0]

    try:
        from app.config import load_config
        from app.infrastructure.cache.auth_token_cache import AuthTokenCache
        from app.infrastructure.cache.redis_cache import RedisCache

        config = load_config(allow_stub_telegram=True)
        if not config.redis.enabled:
            return None

        if _redis_cache_holder[0] is None:
            _redis_cache_holder[0] = RedisCache(config)

        _auth_token_cache_holder[0] = AuthTokenCache(_redis_cache_holder[0], config)
        return _auth_token_cache_holder[0]
    except Exception as exc:
        logger.warning(
            "auth_token_cache_init_failed",
            extra={"error": str(exc)},
        )
        return None


def get_auth_repository() -> Any:
    """Dependency to get auth repository with optional Redis caching.

    Returns:
        Auth repository with token cache if Redis is available.
    """
    token_cache = _get_auth_token_cache()
    return get_db_auth_repository(token_cache=token_cache)


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> AuthenticatedUser:
    """
    Dependency to get current authenticated user.

    Supports two auth methods:
    1. JWT Bearer token (mobile app / API clients)
    2. Telegram WebApp initData (Mini App -- set by webapp_auth_middleware)

    When both are present, JWT takes precedence.

    Raises:
        TokenExpiredError: Access token has expired (401)
        TokenInvalidError: Token is malformed (401)
        TokenWrongTypeError: Not an access token (401)
        AuthorizationError: User not in whitelist (403)
        AuthenticationError: No valid auth method found (401)
    """
    # Check WebApp auth first (set by webapp_auth_middleware)
    webapp_user = getattr(request.state, "webapp_user", None)

    # If we have JWT credentials, use JWT auth (takes precedence)
    if credentials is not None:
        from app.api.exceptions import TokenInvalidError

        token = credentials.credentials
        payload = decode_token(token, expected_type="access")

        user_id = payload.get("user_id")
        if not user_id:
            raise TokenInvalidError("Missing user_id in token payload")

        # All four auth paths (JWT, WebApp, Telegram-Login, secret-login) are
        # fail-closed when ALLOWED_USER_IDS is empty. Unifying around the
        # secure default closes the gap previously opened by JWT-only deploys
        # that instantiate Settings(allow_stub_telegram=True) (the lazy-load
        # default in secret_auth._get_cfg) — that path bypasses the startup
        # validator at app/config/settings.py:315-323. Project is owner-only
        # per CLAUDE.md, so multi-user "fail-open" was never the design intent.
        if not Config.is_user_allowed(user_id, fail_open_when_empty=False):
            raise AuthorizationError("User not authorized")

        try:
            from app.observability.otel import set_user_id_attr

            set_user_id_attr(user_id)
        except ImportError:
            pass

        # Validate client_id from token
        client_id = payload.get("client_id")
        validate_client_id(client_id)

        return {
            "user_id": user_id,
            "username": payload.get("username"),
            "client_id": client_id,
        }

    # Fall back to WebApp auth
    if webapp_user is not None:
        return {
            "user_id": webapp_user["user_id"],
            "username": webapp_user.get("username"),
            "client_id": "webapp",
        }

    # No valid auth method found
    raise AuthenticationError("Authentication required")


def get_webapp_user(request: Request) -> dict[str, Any]:
    """Dependency to get user from Telegram WebApp initData.

    Validates the X-Telegram-Init-Data header using HMAC-SHA256.
    """
    from app.api.routers.auth.webapp_auth import verify_telegram_webapp_init_data

    init_data = request.headers.get("X-Telegram-Init-Data")
    if not init_data:
        raise AuthenticationError("Missing X-Telegram-Init-Data header")
    return verify_telegram_webapp_init_data(init_data)
