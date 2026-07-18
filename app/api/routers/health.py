"""Enhanced health check router with comprehensive system status.

Provides detailed health information about:
- Database connectivity and health score
- Redis availability
- Circuit breaker states
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Request

from app.api.dependencies.database import get_session_manager
from app.api.models.responses import success_response
from app.api.routers.auth import AuthenticatedUser, get_current_user
from app.api.services.auth_service import AuthService
from app.config import load_config
from app.core.logging_utils import get_logger
from app.core.time_utils import UTC, format_iso_z
from app.di.api import resolve_api_runtime

logger = get_logger(__name__)

router = APIRouter()
_DATABASE_DETAILS_TTL_SECONDS = 30.0


class _DatabaseDetailsCache:
    """Tiny TTL cache for the expensive database diagnostics call.

    Encapsulates the two legacy globals (_database_details_cache,
    _database_details_cached_at) so health.py no longer needs the
    `global` keyword.
    """

    def __init__(self) -> None:
        self.value: dict[str, Any] | None = None
        self.cached_at: float = 0.0
        self.lock = asyncio.Lock()

    def fresh(self, now: float) -> bool:
        return self.value is not None and now - self.cached_at < _DATABASE_DETAILS_TTL_SECONDS

    def clear(self) -> None:
        self.value = None
        self.cached_at = 0.0


_database_details = _DatabaseDetailsCache()


def clear_health_check_cache() -> None:
    """Reset cached component details used by health endpoints."""
    _database_details.clear()


async def _compute_database_details(db: Any) -> dict[str, Any]:
    """Compute heavier PostgreSQL database diagnostics using the shared runtime."""
    size_mb = await db.inspection.async_database_size_mb()
    size_bytes = int(size_mb * 1024 * 1024)
    integrity_ok, integrity_result = await db.inspection.async_check_integrity()

    result: dict[str, Any] = {
        "size_bytes": size_bytes,
        "size_mb": size_mb,
        "integrity_ok": integrity_ok,
    }
    if not integrity_ok:
        result["integrity_detail"] = integrity_result
    return result


async def _get_cached_database_details(request: Request | None = None) -> dict[str, Any]:
    """Return cached database diagnostics, recomputing only when stale."""
    now = time.monotonic()
    if _database_details.fresh(now):
        assert _database_details.value is not None  # narrowed by fresh()
        return dict(_database_details.value)

    async with _database_details.lock:
        now = time.monotonic()
        if _database_details.fresh(now):
            assert _database_details.value is not None
            return dict(_database_details.value)

        _db: Any = None
        with contextlib.suppress(RuntimeError):
            _db = resolve_api_runtime(request).db
        if _db is None:
            _db = get_session_manager()
        details = await _compute_database_details(_db)
        _database_details.value = details
        _database_details.cached_at = now
        return dict(details)


async def _check_database(
    *,
    include_details: bool = True,
    request: Request | None = None,
) -> dict[str, Any]:
    """Check database connectivity and health."""
    start = time.perf_counter()
    try:
        db: Any = None
        with contextlib.suppress(RuntimeError):
            db = resolve_api_runtime(request).db
        if db is None:
            db = get_session_manager()
        await db.healthcheck()
        latency_ms = (time.perf_counter() - start) * 1000

        result: dict[str, Any] = {
            "status": "healthy",
            "latency_ms": round(latency_ms, 2),
        }
        if include_details:
            result.update(await _get_cached_database_details(request))
        return result
    except Exception as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        logger.warning(
            "health_check_db_failed",
            extra={"error": str(exc), "latency_ms": latency_ms},
        )
        result = {
            "status": "unhealthy",
            "latency_ms": round(latency_ms, 2),
        }
        # Only expose the raw DB error on the authenticated, detailed path. The
        # unauthenticated readiness/liveness probes must not leak DB host/schema
        # to network-reachable callers (CWE-209); it's logged server-side above.
        if include_details:
            result["error"] = str(exc)
        return result


async def _check_redis() -> dict[str, Any]:
    """Check Redis connectivity using shared client."""
    start = time.perf_counter()
    try:
        from app.infrastructure.redis import get_connection_state, get_redis

        config = load_config()
        if not config.redis.enabled:
            return {"status": "disabled", "latency_ms": 0}

        # Get connection state for detailed reporting
        conn_state = get_connection_state()

        redis_client = await get_redis(config)
        latency_ms = (time.perf_counter() - start) * 1000

        if redis_client is None:
            result: dict[str, Any] = {
                "status": "unavailable",
                "latency_ms": round(latency_ms, 2),
            }
            # Add connection state details
            last_attempt = conn_state["last_attempt"]
            last_error = conn_state["last_error"]
            if isinstance(last_attempt, float) and last_attempt > 0:
                result["last_attempt"] = format_iso_z(datetime.fromtimestamp(last_attempt, tz=UTC))
            if isinstance(last_error, str) and last_error:
                result["error"] = last_error
            return result

        ping_result = redis_client.ping()
        if asyncio.iscoroutine(ping_result):
            async with asyncio.timeout(5.0):
                await ping_result
        latency_ms = (time.perf_counter() - start) * 1000
        return {
            "status": "healthy",
            "latency_ms": round(latency_ms, 2),
        }
    except Exception as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        logger.debug(
            "health_check_redis_failed",
            extra={"error": str(exc), "latency_ms": latency_ms},
        )
        return {
            "status": "unhealthy",
            "error": str(exc),
            "latency_ms": round(latency_ms, 2),
        }


async def _check_scraper() -> dict[str, Any]:
    """Return scraper configuration diagnostics."""
    start = time.perf_counter()
    try:
        from app.adapters.content.scraper.diagnostics import build_scraper_diagnostics

        config = load_config()
        diagnostics = build_scraper_diagnostics(config)
        diagnostics["latency_ms"] = round((time.perf_counter() - start) * 1000, 2)
        return diagnostics
    except Exception as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        logger.debug(
            "health_check_scraper_failed",
            extra={"error": str(exc), "latency_ms": latency_ms},
        )
        return {
            "status": "unhealthy",
            "error": str(exc),
            "latency_ms": round(latency_ms, 2),
        }


async def _check_vector_store(request: Request | None = None) -> dict[str, Any]:
    """Check vector store (Qdrant) availability using the shared store instance."""
    start = time.perf_counter()
    try:
        vector_store: Any = None
        with contextlib.suppress(RuntimeError):
            runtime = resolve_api_runtime(request)
            vector_store = runtime.search.vector_store

        if vector_store is None:
            return {"status": "disabled", "latency_ms": 0}

        latency_ms = (time.perf_counter() - start) * 1000
        if vector_store.available:
            return {
                "status": "healthy",
                "collection": vector_store.collection_name,
                "latency_ms": round(latency_ms, 2),
            }
        return {
            "status": "unavailable",
            "url": vector_store._url,
            "latency_ms": round(latency_ms, 2),
        }
    except Exception as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        logger.debug("health_check_vector_store_failed", extra={"error": str(exc)})
        return {
            "status": "unhealthy",
            "error": str(exc),
            "latency_ms": round(latency_ms, 2),
        }


def _get_circuit_breaker_states() -> dict[str, str]:
    """Get circuit breaker summary.

    Circuit breakers are managed internally by LLM and scraper clients
    (see ``app/utils/circuit_breaker.py``).  Exposing per-client state
    requires DI wiring that is not yet available in the health router.
    """
    return {"status": "managed_by_clients"}


@router.get("/health/detailed")
async def detailed_health_check(
    request: Request, user: AuthenticatedUser = Depends(get_current_user)
) -> Any:
    """Owner-only comprehensive health check with component status.

    Returns detailed status of all system components:
    - Database connectivity and size
    - Redis availability
    - Circuit breaker states
    - Overall health score
    """
    await AuthService.require_owner(user)
    start_time = time.perf_counter()

    # Run component checks concurrently
    try:
        async with asyncio.timeout(10.0):
            db_status, redis_status, scraper_status, vector_status = await asyncio.gather(
                _check_database(include_details=True, request=request),
                _check_redis(),
                _check_scraper(),
                _check_vector_store(request),
                return_exceptions=True,
            )
    except TimeoutError:
        db_status = {"status": "timeout", "error": "Health check timed out"}
        redis_status = {"status": "timeout", "error": "Health check timed out"}
        scraper_status = {"status": "timeout", "error": "Health check timed out"}
        vector_status = {"status": "timeout", "error": "Health check timed out"}

    # Handle exceptions from gather
    if isinstance(db_status, BaseException):
        db_status = {"status": "error", "error": str(db_status)}
    if isinstance(redis_status, BaseException):
        redis_status = {"status": "error", "error": str(redis_status)}
    if isinstance(scraper_status, BaseException):
        scraper_status = {"status": "error", "error": str(scraper_status)}
    if isinstance(vector_status, BaseException):
        vector_status = {"status": "error", "error": str(vector_status)}

    circuit_breaker_states = _get_circuit_breaker_states()

    # Calculate health score
    health_score = 0.0

    db_healthy = db_status.get("status") == "healthy"
    redis_healthy = redis_status.get("status") in ("healthy", "disabled")

    if db_healthy:
        health_score += 50.0
    if redis_healthy:
        health_score += 50.0

    # Overall status
    overall_status = "healthy"
    if health_score < 100:
        overall_status = "degraded"
    if health_score < 50:
        overall_status = "unhealthy"

    total_latency_ms = (time.perf_counter() - start_time) * 1000

    return success_response(
        data={
            "status": overall_status,
            "health_score": health_score,
            "timestamp": format_iso_z(datetime.now(UTC)),
            "total_latency_ms": round(total_latency_ms, 2),
            "components": {
                "database": db_status,
                "redis": redis_status,
                "scraper": scraper_status,
                "vector_store": vector_status,
                "circuit_breakers": circuit_breaker_states,
            },
        }
    )


@router.get("/health/ready")
async def readiness_check() -> Any:
    """Kubernetes readiness probe — intentionally unauthenticated.

    Returns 200 if the service is ready to handle requests.
    Checks database connectivity only. No auth required: probes are
    called by the orchestrator before user traffic is routed, so
    enforcing auth would prevent startup and break pod scheduling.
    """
    db_status = await _check_database(include_details=False)

    if db_status.get("status") == "healthy":
        return success_response(
            data={
                "ready": True,
                "timestamp": format_iso_z(datetime.now(UTC)),
            }
        )

    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=503,
        content={
            "ready": False,
            # Generic message only — never surface the underlying DB error to
            # unauthenticated probe callers.
            "error": "Database not ready",
            "timestamp": format_iso_z(datetime.now(UTC)),
        },
    )


@router.get("/health/live")
async def liveness_check() -> Any:
    """Kubernetes liveness probe — intentionally unauthenticated.

    Returns 200 if the service is running. No auth required: probes
    are called by the orchestrator to detect hangs/crashes and cannot
    carry credentials.
    Minimal check - just verifies the process is responsive.
    """
    return success_response(
        data={
            "alive": True,
            "timestamp": format_iso_z(datetime.now(UTC)),
        }
    )
