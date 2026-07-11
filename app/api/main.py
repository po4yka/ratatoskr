"""
FastAPI application for Ratatoskr Mobile API.

Usage:
    uvicorn app.api.main:app --reload --host 0.0.0.0 --port 8000
"""

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path as _Path
from typing import Any
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.error_handlers import (
    api_exception_handler,
    database_exception_handler,
    global_exception_handler as global_error_handler,
    validation_exception_handler,
)
from app.api.exceptions import APIException
from app.api.middleware import (
    correlation_id_middleware,
    rate_limit_middleware,
    security_headers_middleware,
    webapp_auth_middleware,
)
from app.api.models.responses import success_response
from app.api.models.responses.common import API_CONTRACT_VERSION
from app.api.routers import (
    admin,
    aggregation,
    ai_backups,
    auth,
    backups,
    collections,
    custom_digests,
    digest,
    export_integrations,
    git_mirrors,
    health,
    highlights,
    import_export,
    meta,
    notifications,
    operation_streams,
    proxy,
    quick_save,
    repositories,
    requests,
    rss,
    rules,
    search,
    signals,
    social_auth,
    streams,
    summaries,
    sync,
    system,
    tags,
    tts,
    user,
    webhooks,
)
from app.api.routers.auth import apple as apple_auth, get_current_user, github as github_auth
from app.config import Config
from app.core.logging_utils import get_logger, setup_json_logging
from app.core.time_utils import UTC
from app.di.api import (
    build_api_runtime,
    clear_current_api_runtime,
    close_api_runtime,
    set_current_api_runtime,
)
from app.infrastructure.redis import close_redis

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    runtime = None
    broker = None
    checkpointer_runtime = None
    durable_worker = None
    transcription_worker = None
    try:
        from app.config import load_config as _load_config
        from app.observability.otel import init_tracing

        _cfg = _load_config(allow_stub_telegram=True)
        init_tracing(_cfg, fastapi_app=app)

        # Initialize Sentry when DSN is configured; no-op otherwise.
        if _cfg.sentry.sentry_dsn:
            try:
                import sentry_sdk
                from sentry_sdk.integrations.fastapi import FastApiIntegration
                from sentry_sdk.integrations.loguru import LoguruIntegration

                sentry_sdk.init(
                    dsn=_cfg.sentry.sentry_dsn,
                    integrations=[FastApiIntegration(), LoguruIntegration()],
                    traces_sample_rate=_cfg.sentry.traces_sample_rate,
                )
                logger.info("sentry_initialized")
            except ImportError:
                logger.warning(
                    "sentry_sdk not installed; install monitoring extra to enable Sentry"
                )

        # Start the durable saver before graph construction so every URL processor
        # compiled by the API runtime receives the same Postgres checkpointer.
        if _cfg.langgraph_checkpoint.enabled:
            try:
                from app.infrastructure.checkpointing import CheckpointerRuntime

                checkpointer_runtime = CheckpointerRuntime(cfg=_cfg)
                await checkpointer_runtime.start()
            except ImportError:
                logger.warning("langgraph_checkpointer_not_installed")
            except Exception:
                logger.exception("langgraph_checkpointer_startup_failed")
                checkpointer_runtime = None

        runtime = await build_api_runtime(
            _cfg,
            checkpointer=checkpointer_runtime.saver if checkpointer_runtime is not None else None,
        )
        setup_json_logging(runtime.cfg.runtime.log_level)

        from app.api.routers.auth.tokens import log_auth_posture_summary

        log_auth_posture_summary(runtime.cfg, cors_origins_count=len(_ALLOWED_ORIGINS))

        app.state.runtime = runtime
        set_current_api_runtime(runtime)

        logger.info("database_initialized", extra={"database": "postgresql"})

        from app.adapters.external.formatting.export_temp_files import cleanup_stale_export_files

        export_temp_max_age = runtime.cfg.retention.export_temp_file_max_age_seconds
        if export_temp_max_age > 0:
            cleanup_stale_export_files(max_age_seconds=export_temp_max_age)
        await runtime.durable_request_queue.reconcile_startup()
        if runtime.durable_transcription_queue is not None:
            await runtime.durable_transcription_queue.reconcile_startup()
        if runtime.cfg.background.durable_worker_enabled:
            durable_worker = await runtime.durable_request_queue.start()
            logger.info("durable_request_processing_worker_started")
            if runtime.durable_transcription_queue is not None:
                transcription_worker = await runtime.durable_transcription_queue.start()
                logger.info("durable_transcription_worker_started")

        # Connect the taskiq broker in producer mode so API endpoints can
        # enqueue tasks via .kiq() in future features.
        try:
            from app.tasks.broker import broker as _broker

            if not _broker.is_worker_process:
                await _broker.startup()
                broker = _broker
        except ImportError:
            pass

        yield
    finally:
        if durable_worker is not None:
            await runtime.durable_request_queue.stop()
        if transcription_worker is not None:
            await runtime.durable_transcription_queue.stop()
        if broker is not None and not broker.is_worker_process:
            await broker.shutdown()
        await close_redis()
        if runtime is not None:
            await close_api_runtime(runtime)
            clear_current_api_runtime()
        if checkpointer_runtime is not None:
            await checkpointer_runtime.stop(timeout=10.0)
        logger.info("database_closed")


# FastAPI app instance
_docs_enabled = os.getenv("API_DOCS_ENABLED", "").lower() in ("1", "true")
app = FastAPI(
    title="Ratatoskr Mobile API",
    description="RESTful API for Android/iOS mobile clients",
    version=API_CONTRACT_VERSION,
    servers=[
        {"url": "https://ratatoskrapi.po4yka.com", "description": "Production"},
        {"url": "https://staging-ratatoskrapi.po4yka.com", "description": "Staging"},
        {"url": "http://localhost:8000", "description": "Local development"},
    ],
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    lifespan=lifespan,
)


def _resolve_allowed_origins() -> list[str]:
    """Read ALLOWED_ORIGINS at call time rather than import time."""
    raw = Config.get("ALLOWED_ORIGINS", "").split(",")
    origins = [o.strip() for o in raw if o.strip()]
    if not origins:
        logger.warning(
            "ALLOWED_ORIGINS not configured - defaulting to localhost only. "
            "Set ALLOWED_ORIGINS environment variable for production."
        )
        return [
            "http://localhost:3000",
            "http://localhost:5173",
            "http://localhost:8080",
            "http://127.0.0.1:3000",
            "http://127.0.0.1:5173",
            "http://127.0.0.1:8080",
        ]
    logger.info("cors_allowed_origins_configured", extra={"cors_origins_count": len(origins)})
    return origins


_ALLOWED_ORIGINS = _resolve_allowed_origins()


def _resolve_trusted_hosts() -> list[str]:
    """Allowed Host header values for TrustedHostMiddleware.

    Read TRUSTED_HOSTS (comma-separated) when set. Otherwise default to the
    declared deployment hosts (the OpenAPI ``servers`` URLs) plus the configured
    CORS origin hosts and local/test hosts, so legitimate deployments keep
    working while still rejecting Host-header injection. ``*`` is intentionally
    NOT the default; set TRUSTED_HOSTS='*' to disable the check explicitly.
    """
    raw = Config.get("TRUSTED_HOSTS", "")
    explicit = [h.strip() for h in raw.split(",") if h.strip()]
    if explicit:
        return explicit

    hosts: set[str] = {"localhost", "127.0.0.1", "testserver"}
    for url in (
        "https://ratatoskrapi.po4yka.com",
        "https://staging-ratatoskrapi.po4yka.com",
    ):
        host = urlparse(url).hostname
        if host:
            hosts.add(host)
    for origin in _ALLOWED_ORIGINS:
        host = urlparse(origin).hostname
        if host:
            hosts.add(host)
    logger.warning(
        "trusted_hosts_defaulted",
        extra={"trusted_hosts_count": len(hosts), "hint": "set TRUSTED_HOSTS to restrict"},
    )
    return sorted(hosts)


# Reject requests with an unexpected Host header (host-header injection,
# cache poisoning, poisoned redirect/callback URLs) before any route handler
# runs.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=_resolve_trusted_hosts())


# CORS middleware with specific origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=[
        "GET",
        "POST",
        "PATCH",
        "DELETE",
        "OPTIONS",
        "HEAD",
    ],  # Explicit methods
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-Correlation-ID",
        "X-Telegram-Init-Data",
    ],  # Specific headers
    max_age=3600,  # Cache preflight for 1 hour
)

# Custom middleware (order: last added = outermost = runs first)
# correlation_id must run first, then auth, then rate limit. security_headers is
# added last so it is the outermost layer and stamps headers on every response,
# including rate-limit (429) and error responses.
app.middleware("http")(rate_limit_middleware)
app.middleware("http")(webapp_auth_middleware)
app.middleware("http")(correlation_id_middleware)
app.middleware("http")(security_headers_middleware)

# Include routers
app.include_router(auth.router, prefix="/v1/auth", tags=["Authentication"])
app.include_router(apple_auth.router, prefix="/v1/auth", tags=["Authentication"])

app.include_router(github_auth.router)
app.include_router(aggregation.router, prefix="/v1/aggregations", tags=["Aggregations"])
app.include_router(collections.router, prefix="/v1/collections", tags=["Collections"])
app.include_router(
    collections.public_router, prefix="/v1/public/collections", tags=["Public Collections"]
)
app.include_router(summaries.router, prefix="/v1/summaries", tags=["Summaries"])
app.include_router(repositories.router)
app.include_router(git_mirrors.router)
app.include_router(ai_backups.router)
app.include_router(summaries.router, prefix="/v1/articles", tags=["Articles"])
app.include_router(requests.router, prefix="/v1/requests", tags=["Requests"])
app.include_router(streams.router, prefix="/v1/requests", tags=["Streams"])
app.include_router(search.router, prefix="/v1", tags=["Search"])
app.include_router(signals.router, prefix="/v1/signals", tags=["Signals"])
app.include_router(social_auth.router)
app.include_router(sync.router, prefix="/v1/sync", tags=["Sync"])
app.include_router(user.router, prefix="/v1/user", tags=["User"])
app.include_router(user.profile_router, prefix="/v1/users", tags=["User"])
app.include_router(system.router, prefix="/v1/system", tags=["System"])
app.include_router(proxy.router, prefix="/v1/proxy", tags=["Proxy"])
app.include_router(notifications.router, prefix="/v1/notifications", tags=["Notifications"])
app.include_router(digest.router, prefix="/v1/digest", tags=["Digest"])
app.include_router(custom_digests.router, prefix="/v1/digests/custom", tags=["custom-digests"])
app.include_router(tags.router, prefix="/v1/tags", tags=["Tags"])
app.include_router(tags.summary_tags_router, prefix="/v1/summaries", tags=["Tags"])
app.include_router(tags.summary_tags_router, prefix="/v1/articles", tags=["Article Tags"])
app.include_router(webhooks.router, prefix="/v1/webhooks", tags=["Webhooks"])
app.include_router(backups.router, prefix="/v1/backups", tags=["Backups"])
app.include_router(rules.router, prefix="/v1/rules", tags=["Rules"])
app.include_router(export_integrations.router, prefix="/v1", tags=["Export Integrations"])
app.include_router(import_export.router, prefix="/v1", tags=["Import/Export"])
app.include_router(meta.router, prefix="/v1", tags=["Meta"])
app.include_router(quick_save.router, prefix="/v1", tags=["Quick Save"])
app.include_router(operation_streams.router, prefix="/v1", tags=["Operation Streams"])
app.include_router(highlights.router, prefix="/v1/summaries", tags=["Highlights"])
app.include_router(highlights.router, prefix="/v1/articles", tags=["Article Highlights"])
app.include_router(tts.router, prefix="/v1/summaries", tags=["TTS"])
app.include_router(tts.router, prefix="/v1/articles", tags=["Article TTS"])
app.include_router(tts.preferences_router, prefix="/v1/users", tags=["TTS"])
app.include_router(rss.router, prefix="/v1/rss", tags=["RSS"])
app.include_router(admin.router, prefix="/v1/admin", tags=["Admin"])
app.include_router(health.router, tags=["Health"])

# Serve static files (Mini App HTML for session init, etc.)
_static_dir = _Path(__file__).resolve().parent.parent / "static"
if _static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

_web_index = _static_dir / "web" / "index.html"


def _serve_web_index() -> FileResponse:
    if not _web_index.is_file():
        raise HTTPException(status_code=404, detail="Web interface is not built")
    return FileResponse(str(_web_index))


@app.get("/manifest.webmanifest", include_in_schema=False)
def web_manifest() -> FileResponse:
    """Serve PWA manifest at the SPA's scope root."""
    _manifest = _static_dir / "web" / "manifest.webmanifest"
    if not _manifest.is_file():
        raise HTTPException(status_code=404, detail="Web manifest not found")
    return FileResponse(str(_manifest), media_type="application/manifest+json")


@app.get("/privacy.html", include_in_schema=False)
def privacy_policy() -> FileResponse:
    """Serve Privacy Policy static page."""
    _privacy = _static_dir / "web" / "privacy.html"
    if not _privacy.is_file():
        raise HTTPException(status_code=404, detail="Privacy policy page not found")
    return FileResponse(str(_privacy))


@app.get("/terms.html", include_in_schema=False)
def terms_of_service() -> FileResponse:
    """Serve Terms of Service static page."""
    _terms = _static_dir / "web" / "terms.html"
    if not _terms.is_file():
        raise HTTPException(status_code=404, detail="Terms of service page not found")
    return FileResponse(str(_terms))


@app.get("/api", include_in_schema=False)
def api_root(request: Request) -> dict[str, Any]:
    """Mobile API root endpoint."""
    return success_response(
        {
            "service": "Ratatoskr Mobile API",
            "version": app.version,
            "docs": "/docs",
            "health": "/health",
        },
        correlation_id=getattr(request.state, "correlation_id", None),
    )


@app.get("/health")
def health_check(request: Request) -> dict[str, Any]:
    """Health check endpoint."""
    return success_response(
        {
            "status": "healthy",
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        },
        correlation_id=getattr(request.state, "correlation_id", None),
    )


@app.get("/metrics")
async def metrics(user: dict[str, Any] = Depends(get_current_user)) -> Any:
    """Prometheus metrics endpoint (owner-only).

    Returns metrics in Prometheus text format for scraping.
    """
    from fastapi.responses import Response

    from app.api.services.auth_service import AuthService
    from app.observability.metrics import get_metrics, get_metrics_content_type

    await AuthService.require_owner(user)  # type: ignore[arg-type]
    return Response(
        content=get_metrics(),
        media_type=get_metrics_content_type(),
    )


# SPA catch-all — MUST be registered last so explicit API routes win.
@app.get("/", include_in_schema=False)
@app.get("/{path:path}", include_in_schema=False)
def web_interface(path: str = "") -> FileResponse:
    """Serve web SPA entrypoint for any non-API path."""
    del path
    return _serve_web_index()


# Register exception handlers
app.add_exception_handler(APIException, api_exception_handler)
app.add_exception_handler(
    PydanticValidationError,
    validation_exception_handler,
)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(SQLAlchemyError, database_exception_handler)
app.add_exception_handler(Exception, global_error_handler)


if __name__ == "__main__":
    import uvicorn

    # Development server - bind to all interfaces for Docker/container access
    uvicorn.run(
        "app.api.main:app",
        host="0.0.0.0",  # nosec B104 - intentional for Docker
        port=8000,
        reload=True,
        log_level="info",
    )
