"""FastAPI middleware for request processing."""

from __future__ import annotations

import ipaddress
import json
import os
import time
from typing import TYPE_CHECKING, Any, cast

from fastapi.responses import JSONResponse

from app.api.context import correlation_id_ctx
from app.api.exceptions import ErrorType
from app.api.local_rate_limiter import LocalRateLimiter as _LocalRateLimiter
from app.api.models.responses import error_response, make_error
from app.config import AppConfig, load_config
from app.core.logging_utils import get_logger, sanitize_correlation_id
from app.infrastructure.redis import get_redis, redis_key

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import Request
    from starlette.responses import Response

logger = get_logger(__name__)

# Cached config for middleware usage. Lazy-initialized on the first
# request rather than at startup because (1) middleware is loaded
# before lifespan runs, and (2) config only needs to be read once.
# Wrapped in a single-element list so the mutation site doesn't need
# `global`.
_cfg_holder: list[AppConfig | None] = [None]

# Redis-unavailable warning is re-armed every _REDIS_WARN_INTERVAL_SEC so a
# sustained or recurring outage keeps producing an alertable log line instead of
# being silenced after the first occurrence for the rest of the process life.
# Holds the last-emitted monotonic-ish timestamp (time.time()).
_REDIS_WARN_INTERVAL_SEC = 300.0
_redis_warning_last_ts: list[float] = [0.0]

# Auth buckets whose rate limiting is a security control (brute force /
# credential stuffing). In production these MUST NOT silently degrade to the
# per-process in-memory limiter when Redis is down — they fail closed (503).
_REDIS_REQUIRED_AUTH_BUCKETS = frozenset(
    {"secret_login", "credentials_login", "telegram_login", "auth_refresh"}
)

# In-memory rate limiting fallback when Redis is unavailable. The
# singleton lives at module scope so existing tests that import
# `_local_rate_limits` continue to work; the underlying state is now
# encapsulated in LocalRateLimiter so middleware no longer needs the
# `global` keyword.
_local_rate_limiter = _LocalRateLimiter()
# Backward-compatible alias for tests that reach into module state.
_local_rate_limits = _local_rate_limiter._buckets


def _check_local_rate_limit(user_id: str, limit: int, window: int) -> tuple[bool, int]:
    """In-memory rate limit check. Thread-safe.

    Delegates to the module-level :class:`LocalRateLimiter` instance.
    """
    return _local_rate_limiter.check(user_id, limit=limit, window=window)


async def webapp_auth_middleware(request: Request, call_next: Callable[..., Any]) -> Response:
    """Validate Telegram WebApp initData and attach user to request.state.

    When X-Telegram-Init-Data header is present and no Authorization header,
    validates the initData and stores the parsed user in request.state.webapp_user.
    This lets downstream ``get_current_user`` dependency accept WebApp auth
    without modifying every router.
    """
    init_data = request.headers.get("X-Telegram-Init-Data")
    if init_data and "Authorization" not in request.headers:
        try:
            from app.api.routers.auth.webapp_auth import verify_telegram_webapp_init_data

            user = verify_telegram_webapp_init_data(init_data)
            request.state.webapp_user = user
            request.state.user_id = str(user["user_id"])
            try:
                from app.observability.otel import set_user_id_attr

                set_user_id_attr(user["user_id"])
            except ImportError:
                pass
        except Exception as exc:
            request.state.webapp_auth_error = str(exc)
            # WARNING, not DEBUG: forged/replayed WebApp init-data is a security
            # event that must be visible in production logs and alertable. Include
            # the client IP and correlation ID for SIEM correlation.
            logger.warning(
                "webapp_auth_header_parse_failed",
                extra={
                    "error": str(exc),
                    "client_ip": _get_client_ip(request),
                    "correlation_id": getattr(request.state, "correlation_id", None),
                },
            )
    return cast("Response", await call_next(request))


async def correlation_id_middleware(request: Request, call_next: Callable[..., Any]) -> Response:
    """
    Add correlation ID to all requests for tracing.

    Validates the incoming X-Correlation-ID header (allowed chars: A-Za-z0-9._:-,
    max 128 chars). Generates a fresh ID when the header is absent or invalid so
    that unsafe values never reach logs or response headers.
    """
    raw = request.headers.get("X-Correlation-ID")
    correlation_id, was_generated = sanitize_correlation_id(raw)

    if not was_generated and raw != correlation_id:
        # Should not happen (sanitize either keeps or replaces), but be explicit.
        was_generated = True

    if was_generated and raw:
        logger.debug(
            "correlation_id_sanitized",
            extra={"reason": "invalid_chars_or_length", "path": request.url.path},
        )

    # Store in request state and context for access in handlers/helpers
    request.state.correlation_id = correlation_id
    token = correlation_id_ctx.set(correlation_id)
    try:
        from app.observability.otel import set_correlation_id_attr

        set_correlation_id_attr(correlation_id)
    except ImportError:
        pass

    try:
        import sentry_sdk

        sentry_sdk.set_tag("correlation_id", correlation_id)
    except ImportError:
        pass

    try:
        response = cast("Response", await call_next(request))
        response.headers["X-Correlation-ID"] = correlation_id
        return response
    finally:
        correlation_id_ctx.reset(token)


# Default CSP frame-ancestors policy. The backend serves the Telegram WebApp,
# which Telegram renders inside its own (cross-origin) iframe, so we cannot use
# X-Frame-Options: DENY / frame-ancestors 'none' (that would break the WebApp).
# Instead we allowlist self + the Telegram web origins. Override via env if the
# app is embedded elsewhere.
_DEFAULT_FRAME_ANCESTORS = "'self' https://web.telegram.org https://*.telegram.org"
_DEFAULT_PERMISSIONS_POLICY = "geolocation=(), microphone=(), camera=(), payment=()"


async def security_headers_middleware(request: Request, call_next: Callable[..., Any]) -> Response:
    """Attach baseline security response headers to every response.

    Uses ``setdefault`` so a handler that intentionally set its own value wins.
    CSP is limited to ``frame-ancestors`` (clickjacking protection) rather than a
    full ``default-src`` policy so the served SPA's scripts/styles keep loading.
    HSTS is safe to always send: browsers ignore it over plain HTTP.
    """
    response = cast("Response", await call_next(request))
    headers = response.headers
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("Referrer-Policy", "no-referrer")
    headers.setdefault(
        "Permissions-Policy",
        os.getenv("PERMISSIONS_POLICY", _DEFAULT_PERMISSIONS_POLICY),
    )
    frame_ancestors = os.getenv("CSP_FRAME_ANCESTORS", _DEFAULT_FRAME_ANCESTORS)
    headers.setdefault("Content-Security-Policy", f"frame-ancestors {frame_ancestors}")
    headers.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
    return response


def _get_cfg() -> AppConfig:
    if _cfg_holder[0] is None:
        _cfg_holder[0] = load_config(allow_stub_telegram=True)
    return _cfg_holder[0]


def _resolve_limit(_path: str, cfg: AppConfig) -> int:
    return _resolve_limit_from_bucket(cfg=cfg, bucket=None)


def _resolve_limit_from_bucket(cfg: AppConfig, bucket: str | None) -> int:
    limits = cfg.api_limits
    if bucket == "aggregation_create":
        return limits.aggregation_create_user_limit
    if bucket == "secret_login":
        return limits.secret_login_limit
    if bucket == "credentials_login":
        return limits.credentials_login_limit
    if bucket == "summaries":
        return limits.summaries_limit
    if bucket == "requests":
        return limits.requests_limit
    if bucket == "search":
        return limits.search_limit
    if bucket and bucket.startswith("auth_"):
        return limits.auth_limit
    return limits.default_limit


def _get_auth_context_from_auth_header(request: Request) -> tuple[str | None, str | None]:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return None, None
    token = auth_header.split(" ", 1)[1].strip()
    if not token:
        return None, None
    try:
        from app.api.routers.auth import decode_token

        payload = decode_token(token, expected_type="access")
    except Exception:
        logger.warning("jwt_decode_failed_for_rate_limit")
        return None, None
    user_id = payload.get("user_id")
    client_id = payload.get("client_id")
    normalized_client_id = client_id.strip() if isinstance(client_id, str) else None
    if isinstance(user_id, int):
        return str(user_id), normalized_client_id or None
    if isinstance(user_id, str) and user_id.isdigit():
        return user_id, normalized_client_id or None
    return None, normalized_client_id or None


# Parsed TRUSTED_PROXY_IPS, cached keyed by the raw env string so a changed
# value is re-parsed without a restart. Single-element list to avoid `global`.
_trusted_proxies_holder: list[tuple[str, list[Any]] | None] = [None]


def _trusted_proxy_networks() -> list[Any]:
    """Parse TRUSTED_PROXY_IPS (comma-separated IPs/CIDRs) into ip_network objects."""
    raw = os.getenv("TRUSTED_PROXY_IPS", "")
    cached = _trusted_proxies_holder[0]
    if cached is not None and cached[0] == raw:
        return cached[1]
    nets: list[Any] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            nets.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            logger.warning("invalid_trusted_proxy_ip", extra={"value": token})
    _trusted_proxies_holder[0] = (raw, nets)
    return nets


def _ip_in_networks(ip_str: str, nets: list[Any]) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(ip in net for net in nets)


def _get_client_ip(request: Request) -> str:
    """Resolve the client IP for rate limiting.

    X-Forwarded-For is honored ONLY when the direct peer is a configured trusted
    proxy (TRUSTED_PROXY_IPS). Without that, the header is attacker-controlled
    and ignored. When trusted, the right-most XFF entry that is not itself a
    trusted proxy is the real client (so per-IP limits work behind a proxy
    instead of collapsing every client into the proxy's single bucket).
    """
    direct = request.client.host if request.client else None
    nets = _trusted_proxy_networks()
    if direct and nets and _ip_in_networks(direct, nets):
        xff = request.headers.get("X-Forwarded-For")
        if xff:
            for hop in reversed([h.strip() for h in xff.split(",") if h.strip()]):
                if not _ip_in_networks(hop, nets):
                    return hop
    return direct or "unknown"


async def _extract_json_body(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    if content_type and "application/json" not in content_type.lower():
        return {}
    try:
        raw = await request.body()
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def _resolve_auth_body_client_id(request: Request, bucket: str | None) -> str | None:
    if bucket not in {"credentials_login", "secret_login", "telegram_login"}:
        return None
    body = await _extract_json_body(request)
    client_id = body.get("client_id")
    return client_id.strip() if isinstance(client_id, str) and client_id.strip() else None


async def _resolve_refresh_client_id(request: Request, bucket: str | None) -> str | None:
    if bucket != "auth_refresh":
        return None
    body = await _extract_json_body(request)
    token = body.get("refresh_token")
    if not isinstance(token, str) or not token.strip():
        try:
            from app.api.routers.auth.cookies import REFRESH_COOKIE_NAME

            token = request.cookies.get(REFRESH_COOKIE_NAME)
        except Exception:
            token = None
    if not isinstance(token, str) or not token.strip():
        return None
    try:
        from app.api.routers.auth import decode_token

        payload = decode_token(token.strip(), expected_type="refresh")
    except Exception:
        logger.warning("refresh_jwt_decode_failed_for_rate_limit")
        return None
    client_id = payload.get("client_id")
    return client_id.strip() if isinstance(client_id, str) and client_id.strip() else None


async def _resolve_rate_limit_context(
    request: Request, bucket: str | None
) -> dict[str, str | None]:
    auth_user_id, auth_client_id = _get_auth_context_from_auth_header(request)
    user_id = getattr(request.state, "user_id", None)
    client_id = getattr(request.state, "client_id", None)

    resolved_user_id = str(user_id) if user_id else auth_user_id
    resolved_client_id = str(client_id) if client_id else auth_client_id
    if not resolved_client_id:
        resolved_client_id = await _resolve_auth_body_client_id(request, bucket)
    if not resolved_client_id:
        resolved_client_id = await _resolve_refresh_client_id(request, bucket)

    if not resolved_user_id or not resolved_client_id:
        webapp_user = getattr(request.state, "webapp_user", None)
        if isinstance(webapp_user, dict):
            if not resolved_user_id:
                webapp_user_id = webapp_user.get("user_id")
                if isinstance(webapp_user_id, int):
                    resolved_user_id = str(webapp_user_id)
                elif isinstance(webapp_user_id, str) and webapp_user_id.isdigit():
                    resolved_user_id = webapp_user_id
            if not resolved_client_id:
                resolved_client_id = "webapp"

    client_ip = _get_client_ip(request)
    actor = resolved_user_id if resolved_user_id else client_ip
    return {
        "actor": actor,
        "user_id": resolved_user_id,
        "client_id": resolved_client_id,
        "client_ip": client_ip,
    }


def _resolve_bucket(method: str, path: str) -> str | None:
    normalized_path = path.rstrip("/") or "/"
    method_upper = method.upper()
    if method_upper == "POST" and normalized_path == "/v1/aggregations":
        return "aggregation_create"
    if method_upper == "POST" and normalized_path == "/v1/auth/secret-login":
        return "secret_login"
    if method_upper == "POST" and normalized_path == "/v1/auth/credentials-login":
        return "credentials_login"
    if method_upper == "POST" and normalized_path == "/v1/auth/telegram-login":
        return "telegram_login"
    if method_upper == "POST" and normalized_path == "/v1/auth/refresh":
        return "auth_refresh"
    if method_upper == "POST" and normalized_path == "/v1/auth/logout":
        return "auth_logout"
    if method_upper == "POST" and normalized_path == "/v1/auth/logout-all":
        return "auth_logout_all"
    if method_upper == "GET" and normalized_path == "/v1/auth/sessions":
        return "auth_sessions_list"
    if method_upper == "DELETE" and normalized_path.startswith("/v1/auth/sessions/"):
        return "auth_session_delete"
    if method_upper == "GET" and normalized_path == "/v1/auth/me":
        return "auth_me"
    if method_upper == "DELETE" and normalized_path == "/v1/auth/me":
        return "auth_delete_account"
    if method_upper == "POST" and normalized_path == "/v1/auth/credentials/change-password":
        return "auth_credentials_change_password"
    if method_upper == "GET" and normalized_path == "/v1/auth/me/telegram":
        return "auth_telegram_status"
    if method_upper == "POST" and normalized_path == "/v1/auth/me/telegram/link":
        return "auth_telegram_link"
    if method_upper == "POST" and normalized_path == "/v1/auth/me/telegram/complete":
        return "auth_telegram_complete"
    if method_upper == "DELETE" and normalized_path == "/v1/auth/me/telegram":
        return "auth_telegram_unlink"
    if method_upper == "POST" and normalized_path == "/v1/auth/secret-keys":
        return "auth_secret_key_create"
    if method_upper == "GET" and normalized_path == "/v1/auth/secret-keys":
        return "auth_secret_key_list"
    if method_upper == "POST" and normalized_path.startswith("/v1/auth/secret-keys/"):
        if normalized_path.endswith("/rotate"):
            return "auth_secret_key_rotate"
        if normalized_path.endswith("/revoke"):
            return "auth_secret_key_revoke"
    if method_upper == "POST" and normalized_path == "/v1/auth/github/pat":
        return "auth_github_pat"
    if method_upper == "GET" and normalized_path == "/v1/auth/github/status":
        return "auth_github_status"
    if method_upper == "POST" and normalized_path == "/v1/auth/github/sync":
        return "auth_github_sync"
    if method_upper == "DELETE" and normalized_path == "/v1/auth/github":
        return "auth_github_disconnect"
    if method_upper == "POST" and normalized_path == "/v1/auth/github/device/start":
        return "auth_github_device_start"
    if method_upper == "POST" and normalized_path == "/v1/auth/github/device/poll":
        return "auth_github_device_poll"
    if "/summaries" in normalized_path:
        return "summaries"
    if "/search" in normalized_path:
        return "search"
    if "/requests" in normalized_path:
        return "requests"
    if "/auth" in normalized_path:
        return "auth_other"
    return None


def _bucket_rate_key(bucket: str | None, actor: str) -> str:
    bucket_name = bucket or "default"
    return f"{bucket_name}:{actor}"


def _auth_client_ip_actor(bucket: str | None, client_id: str | None, client_ip: str) -> str | None:
    if bucket not in {"credentials_login", "secret_login", "telegram_login", "auth_refresh"}:
        return None
    if not client_id:
        return None
    # Only let a client_id refine the rate-limit key if it is in the configured
    # allowlist. Otherwise an attacker could mint a fresh counter per request by
    # rotating arbitrary client_id values in the body (unbounded brute force).
    # Returning None falls back to the IP-based actor (a single bucket per IP).
    try:
        from app.config import Config

        allowed = Config.get_allowed_client_ids()
    except Exception:
        allowed = None
    if not allowed or client_id not in allowed:
        return None
    return f"client_id={client_id}|ip={client_ip}"


def _record_rate_limit_hit(bucket: str | None) -> None:
    try:
        from app.observability.metrics import record_rate_limit_hit

        record_rate_limit_hit(bucket or "default")
    except Exception as exc:
        logger.debug(
            "rate_limit_metric_failed",
            extra={"bucket": bucket or "default", "error": str(exc)},
        )


def _build_rate_limit_response(
    *,
    correlation_id: str | None,
    code: str,
    message: str,
    error_type: ErrorType,
    status_code: int,
    retry_after: int | None = None,
    limit: int | None = None,
    remaining: int | None = None,
    reset: int | None = None,
) -> JSONResponse:
    detail_kwargs: dict[str, Any] = {
        "code": code,
        "message": message,
        "error_type": error_type,
        "retryable": True,
    }
    if retry_after is not None:
        detail_kwargs["details"] = {"retry_after": retry_after}
        detail_kwargs["retry_after"] = retry_after
    detail = make_error(**detail_kwargs)
    detail.correlation_id = correlation_id

    headers: dict[str, str] = {}
    if limit is not None:
        headers["X-RateLimit-Limit"] = str(limit)
    if remaining is not None:
        headers["X-RateLimit-Remaining"] = str(remaining)
    if reset is not None:
        headers["X-RateLimit-Reset"] = str(reset)
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)

    return JSONResponse(
        status_code=status_code,
        content=error_response(detail, correlation_id=correlation_id),
        headers=headers or None,
    )


def _attach_rate_limit_headers(
    *,
    response: Any,
    limit: int,
    remaining: int,
    window_start: int,
    window: int,
) -> Any:
    response.headers["X-RateLimit-Limit"] = str(limit)
    response.headers["X-RateLimit-Remaining"] = str(max(remaining, 0))
    response.headers["X-RateLimit-Reset"] = str(window_start + window)
    return response


def _compute_retry_after(now: int, window_start: int, window: int, cfg: AppConfig) -> int:
    return max((window_start + window) - now, int(window * cfg.api_limits.cooldown_multiplier))


def _log_redis_unavailable_once(cfg: AppConfig, correlation_id: str | None, path: str) -> None:
    now = time.time()
    if now - _redis_warning_last_ts[0] < _REDIS_WARN_INTERVAL_SEC:
        return
    is_prod = cfg.deployment.is_production_mode
    extra: dict[str, Any] = {
        "required": cfg.redis.required,
        "correlation_id": correlation_id,
        "path": path,
        "production_mode": is_prod,
    }
    if is_prod:
        extra["warning"] = (
            "[DEV-ONLY FALLBACK ACTIVE IN PRODUCTION] "
            "Rate limiting is using in-memory state. Limits are not shared across "
            "workers or restarts. Set REDIS_REQUIRED=true in production."
        )
    logger.warning("rate_limit_redis_unavailable", extra=extra)
    _redis_warning_last_ts[0] = now


async def _handle_local_rate_limit(
    *,
    request: Request,
    call_next: Callable[..., Any],
    cfg: AppConfig,
    correlation_id: str | None,
    rate_key: str,
    log_actor: str,
    bucket: str | None,
    bucket_limit: int,
    window: int,
    window_start: int,
    now: int,
) -> JSONResponse | Any:
    allowed, remaining = _check_local_rate_limit(rate_key, bucket_limit, window)
    if not allowed:
        _record_rate_limit_hit(bucket)
        retry_after = _compute_retry_after(now, window_start, window, cfg)
        logger.info(
            "rate_limit_exceeded_local",
            extra={
                "user_id": log_actor,
                "path": request.url.path,
                "limit": bucket_limit,
                "bucket": bucket or "default",
                "retry_after": retry_after,
                "correlation_id": correlation_id,
                "backend": "in-memory",
            },
        )
        return _build_rate_limit_response(
            correlation_id=correlation_id,
            code="RATE_LIMIT_EXCEEDED",
            message=f"Rate limit exceeded. Try again in {retry_after} seconds.",
            error_type=ErrorType.RATE_LIMIT,
            status_code=429,
            retry_after=retry_after,
            limit=bucket_limit,
            remaining=0,
            reset=window_start + window,
        )

    response = await call_next(request)
    return _attach_rate_limit_headers(
        response=response,
        limit=bucket_limit,
        remaining=remaining,
        window_start=window_start,
        window=window,
    )


async def _handle_redis_rate_limit(
    *,
    request: Request,
    call_next: Callable[..., Any],
    cfg: AppConfig,
    correlation_id: str | None,
    redis_client: Any,
    rate_key: str,
    log_actor: str,
    bucket: str | None,
    bucket_limit: int,
    window: int,
    window_start: int,
    now: int,
) -> JSONResponse | Any:
    key = redis_key(cfg.redis.prefix, "rate", rate_key, str(window_start))
    ttl = max(window + 5, int(window * cfg.api_limits.cooldown_multiplier))
    pipe = redis_client.pipeline()
    pipe.incr(key)
    pipe.expire(key, ttl)
    count, _ = await pipe.execute()

    if count > bucket_limit:
        _record_rate_limit_hit(bucket)
        retry_after = _compute_retry_after(now, window_start, window, cfg)
        logger.info(
            "rate_limit_exceeded",
            extra={
                "user_id": log_actor,
                "path": request.url.path,
                "limit": bucket_limit,
                "bucket": bucket or "default",
                "count": count,
                "retry_after": retry_after,
                "correlation_id": correlation_id,
            },
        )
        return _build_rate_limit_response(
            correlation_id=correlation_id,
            code="RATE_LIMIT_EXCEEDED",
            message=f"Rate limit exceeded. Try again in {retry_after} seconds.",
            error_type=ErrorType.RATE_LIMIT,
            status_code=429,
            retry_after=retry_after,
            limit=bucket_limit,
            remaining=0,
            reset=window_start + window,
        )

    response = await call_next(request)
    return _attach_rate_limit_headers(
        response=response,
        limit=bucket_limit,
        remaining=max(bucket_limit - count, 0),
        window_start=window_start,
        window=window,
    )


def _aggregation_client_limit(cfg: AppConfig, bucket: str | None) -> int | None:
    if bucket != "aggregation_create":
        return None
    return cfg.api_limits.aggregation_create_client_limit


async def _enforce_client_limit_local(
    *,
    request: Request,
    cfg: AppConfig,
    correlation_id: str | None,
    client_id: str,
    limit: int,
    window: int,
    window_start: int,
    now: int,
) -> JSONResponse | None:
    allowed, _remaining = _check_local_rate_limit(
        _bucket_rate_key("aggregation_create_client", client_id),
        limit,
        window,
    )
    if allowed:
        return None

    _record_rate_limit_hit("aggregation_create_client")
    retry_after = _compute_retry_after(now, window_start, window, cfg)
    logger.info(
        "aggregation_create_client_rate_limit_exceeded_local",
        extra={
            "client_id": client_id,
            "path": request.url.path,
            "limit": limit,
            "retry_after": retry_after,
            "correlation_id": correlation_id,
            "backend": "in-memory",
        },
    )
    return _build_rate_limit_response(
        correlation_id=correlation_id,
        code="RATE_LIMIT_EXCEEDED",
        message=f"Aggregation client rate limit exceeded. Try again in {retry_after} seconds.",
        error_type=ErrorType.RATE_LIMIT,
        status_code=429,
        retry_after=retry_after,
        limit=limit,
        remaining=0,
        reset=window_start + window,
    )


async def _enforce_client_limit_redis(
    *,
    request: Request,
    cfg: AppConfig,
    correlation_id: str | None,
    redis_client: Any,
    client_id: str,
    limit: int,
    window: int,
    window_start: int,
    now: int,
) -> JSONResponse | None:
    key = redis_key(
        cfg.redis.prefix,
        "rate",
        _bucket_rate_key("aggregation_create_client", client_id),
        str(window_start),
    )
    ttl = max(window + 5, int(window * cfg.api_limits.cooldown_multiplier))
    pipe = redis_client.pipeline()
    pipe.incr(key)
    pipe.expire(key, ttl)
    count, _ = await pipe.execute()

    if count <= limit:
        return None

    _record_rate_limit_hit("aggregation_create_client")
    retry_after = _compute_retry_after(now, window_start, window, cfg)
    logger.info(
        "aggregation_create_client_rate_limit_exceeded",
        extra={
            "client_id": client_id,
            "path": request.url.path,
            "limit": limit,
            "count": count,
            "retry_after": retry_after,
            "correlation_id": correlation_id,
        },
    )
    return _build_rate_limit_response(
        correlation_id=correlation_id,
        code="RATE_LIMIT_EXCEEDED",
        message=f"Aggregation client rate limit exceeded. Try again in {retry_after} seconds.",
        error_type=ErrorType.RATE_LIMIT,
        status_code=429,
        retry_after=retry_after,
        limit=limit,
        remaining=0,
        reset=window_start + window,
    )


async def rate_limit_middleware(request: Request, call_next: Callable[..., Any]) -> Response:
    """Redis-backed rate limiting middleware with graceful fallback."""
    cfg = _get_cfg()
    correlation_id = getattr(request.state, "correlation_id", None)
    path = request.url.path
    bucket = _resolve_bucket(request.method, path)
    rate_limit_context = await _resolve_rate_limit_context(request, bucket)
    actor = rate_limit_context["actor"] or "unknown"
    client_id = rate_limit_context["client_id"]
    client_ip = rate_limit_context["client_ip"] or "unknown"
    auth_actor = _auth_client_ip_actor(bucket, client_id, client_ip)
    rate_actor = auth_actor or actor
    log_actor = rate_limit_context["user_id"] or rate_actor

    request.state.interface_route_key = path
    request.state.interface_route_requires_auth = True

    bucket_limit = _resolve_limit_from_bucket(cfg=cfg, bucket=bucket)
    rate_key = _bucket_rate_key(bucket, rate_actor)
    client_limit = _aggregation_client_limit(cfg, bucket)
    window = cfg.api_limits.window_seconds
    now = int(time.time())
    window_start = (now // window) * window

    redis_client = await get_redis(cfg)
    if redis_client is None:
        _log_redis_unavailable_once(cfg, correlation_id, request.url.path)

        # Fail closed when Redis is required, or when this is a security-sensitive
        # auth bucket in production. The per-process in-memory fallback does not
        # share state across workers/replicas, so allowing auth brute-force
        # buckets through it in production would silently void the protection.
        fail_closed = cfg.redis.required or (
            bucket in _REDIS_REQUIRED_AUTH_BUCKETS and cfg.deployment.is_production_mode
        )
        if fail_closed:
            return _build_rate_limit_response(
                correlation_id=correlation_id,
                code="RATE_LIMIT_BACKEND_UNAVAILABLE",
                message="Rate limit backend unavailable. Please try again later.",
                error_type=ErrorType.INTERNAL,
                status_code=503,
            )
        if client_limit is not None and client_id:
            client_limit_response = await _enforce_client_limit_local(
                request=request,
                cfg=cfg,
                correlation_id=correlation_id,
                client_id=client_id,
                limit=client_limit,
                window=window,
                window_start=window_start,
                now=now,
            )
            if client_limit_response is not None:
                return client_limit_response
        return await _handle_local_rate_limit(
            request=request,
            call_next=call_next,
            cfg=cfg,
            correlation_id=correlation_id,
            rate_key=rate_key,
            log_actor=log_actor,
            bucket=bucket,
            bucket_limit=bucket_limit,
            window=window,
            window_start=window_start,
            now=now,
        )

    if client_limit is not None and client_id:
        client_limit_response = await _enforce_client_limit_redis(
            request=request,
            cfg=cfg,
            correlation_id=correlation_id,
            redis_client=redis_client,
            client_id=client_id,
            limit=client_limit,
            window=window,
            window_start=window_start,
            now=now,
        )
        if client_limit_response is not None:
            return client_limit_response

    return await _handle_redis_rate_limit(
        request=request,
        call_next=call_next,
        cfg=cfg,
        correlation_id=correlation_id,
        redis_client=redis_client,
        rate_key=rate_key,
        log_actor=log_actor,
        bucket=bucket,
        bucket_limit=bucket_limit,
        window=window,
        window_start=window_start,
        now=now,
    )
