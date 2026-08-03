"""MCP server entrypoint and FastMCP composition shell."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp import FastMCP

from app.mcp.aggregation_service import AggregationMcpService
from app.mcp.archive_research_service import ArchiveResearchMcpService
from app.mcp.article_service import ArticleReadService
from app.mcp.catalog_service import CatalogReadService
from app.mcp.context import McpServerContext
from app.mcp.http_auth import McpHttpAuthMiddleware
from app.mcp.resource_registrations import register_resources
from app.mcp.semantic_service import SemanticSearchService
from app.mcp.signal_service import SignalMcpService
from app.mcp.tool_registrations import register_tools
from app.mcp.x_search_service import XSearchService

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

logger = logging.getLogger("ratatoskr.mcp")

_DEFAULT_CONTEXT = McpServerContext(logger=logger)
_UNSCOPED_SSE_LOOPBACK_HOST = "127.0.0.1"
# Environments where unscoped (all-users) MCP SSE may run without the explicit
# MCP_ALLOW_UNSCOPED_PRODUCTION override. Anything else -- including an unset or
# unrecognized APP_ENV -- is treated as non-dev and must opt in, so a forgotten
# APP_ENV cannot silently expose every user's data (fail-safe).
_DEV_ENVS = frozenset({"development", "dev", "test", "testing", "local", "ci"})


def _is_loopback_host(host: str) -> bool:
    return host.strip().lower() in {"127.0.0.1", "::1", "localhost"}


def _env_flag_enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _deployment_env() -> str:
    return os.getenv("APP_ENV", "development").strip().lower() or "development"


# The flush must not outlive the container's stop grace period. BatchSpanProcessor
# joins its worker thread for up to 30 s, and every MCP service is configured with
# `stop_grace_period: 30s`, so an unreachable collector could burn the whole budget
# and then be SIGKILLed with the spans still buffered -- slower shutdowns, no more
# telemetry. That is not a corner case: the default OTEL_EXPORTER_OTLP_ENDPOINT is
# `http://tempo:4317`, and tempo lives in docker-compose.monitoring.yml on its own
# network, so it does not resolve from these containers unless an operator wires it
# up (measured: 7.5 s just to fail DNS, 30 s against a stalled collector). Five
# seconds matches the processor's own scheduling delay -- long enough for a healthy
# local collector, which exports in milliseconds.
_SPAN_FLUSH_TIMEOUT_SEC = 5.0


def _install_tracing_flush(app: Any) -> None:
    """Drain the span buffer from the ASGI lifespan -- the only seam that runs.

    Docker stops these containers with SIGTERM. uvicorn catches it and unwinds
    gracefully, but ``Server.capture_signals()`` restores the original handler
    and re-raises the signal on the way out, so the process dies at SIG_DFL
    *inside* ``uvicorn.run()``. Measured on starlette 1.3.1 / uvicorn 0.51: the
    lifespan shutdown runs, a ``finally`` after ``uvicorn.run()`` does not, and
    neither does ``atexit`` -- which is what ``TracerProvider(shutdown_on_exit=
    True)`` relies on, and the only thing that had ever flushed this process.

    So every span still held by the BatchSpanProcessor (queue up to 2048, 5 s
    scheduling delay, longer when the exporter is slow) was dropped on each
    restart. That tail is what an operator reads first.

    ``shutdown_tracing`` is synchronous and uvicorn waits on this lifespan with
    no timeout of its own, so it runs in a thread under a deadline rather than
    on the loop -- see ``_SPAN_FLUSH_TIMEOUT_SEC``.

    ``run_server`` also has a stdio transport; it is the local-client path and
    is not deployed. It ends when stdin closes, which is a normal interpreter
    exit, so ``atexit`` still covers it.

    Wraps whatever lifespan the MCP SDK installed rather than replacing it. In
    the current SDK that is a no-op, but the object belongs to the SDK.
    """
    previous = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def _lifespan(scope_app: Any) -> AsyncGenerator[Any]:
        # The whole `async with` sits inside the try, so the flush also runs
        # when the wrapped lifespan fails on the way up and when the shutdown is
        # delivered by throwing into this generator rather than by a message.
        try:
            async with previous(scope_app) as state:
                yield state
        finally:
            await _flush_spans()

    app.router.lifespan_context = _lifespan


async def _flush_spans() -> None:
    """Export whatever is buffered, without letting it hold the container open."""
    from app.observability.otel import shutdown_tracing

    try:
        await asyncio.wait_for(asyncio.to_thread(shutdown_tracing), _SPAN_FLUSH_TIMEOUT_SEC)
    except TimeoutError:
        # The thread keeps running; the process usually outlives it long enough
        # to finish. Losing the spans must never also stall the stop.
        logger.warning("mcp_span_flush_timed_out", extra={"timeout_sec": _SPAN_FLUSH_TIMEOUT_SEC})
    except Exception:
        logger.warning("mcp_span_flush_failed", exc_info=True)


def _build_sse_app(
    *,
    mcp_server: FastMCP,
    auth_mode: str,
    forwarded_access_token_header: str,
    forwarded_secret_header: str,
    forwarding_secret: str | None,
) -> Any:
    app: Any = mcp_server.sse_app()
    # Before the auth wrapper: McpHttpAuthMiddleware forwards non-http scopes
    # untouched, so the lifespan reaches this app either way, but the hook
    # belongs to the app that owns the router.
    _install_tracing_flush(app)
    if auth_mode == "jwt":
        app = McpHttpAuthMiddleware(
            app,
            forwarded_access_token_header=forwarded_access_token_header,
            forwarded_secret_header=forwarded_secret_header,
            forwarding_secret=forwarding_secret,
        )
    return app


def create_mcp_server(context: McpServerContext | None = None) -> FastMCP:
    server_context = context or _DEFAULT_CONTEXT
    mcp = FastMCP(
        "ratatoskr",
        instructions=(
            "Ratatoskr is a personal knowledge base of web article summaries. "
            "Use the tools below to search, retrieve, explore stored articles, and "
            "run local trusted aggregation bundles for the effective scoped user. "
            "Articles are summarised with key ideas, topic tags, entities, "
            "reading-time estimates, and more."
        ),
    )

    aggregation_service = AggregationMcpService(server_context)
    article_service = ArticleReadService(server_context)
    catalog_service = CatalogReadService(server_context)
    semantic_service = SemanticSearchService(server_context, article_service)
    signal_service = SignalMcpService(server_context)
    x_search_service_inst = XSearchService(server_context)
    archive_research_service = ArchiveResearchMcpService(
        server_context,
        article_service,
        x_search_service_inst,
    )

    register_tools(
        mcp,
        context=server_context,
        aggregation_service=aggregation_service,
        article_service=article_service,
        catalog_service=catalog_service,
        semantic_service=semantic_service,
        signal_service=signal_service,
        x_search_service_inst=x_search_service_inst,
        archive_research_service=archive_research_service,
    )
    register_resources(
        mcp,
        context=server_context,
        aggregation_service=aggregation_service,
        article_service=article_service,
        catalog_service=catalog_service,
        semantic_service=semantic_service,
        signal_service=signal_service,
    )
    return mcp


mcp = create_mcp_server()


def run_server(
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8200,
    database_dsn: str | None = None,
    user_id: int | None = None,
    auth_mode: str = "disabled",
    forwarded_access_token_header: str = "X-Ratatoskr-Forwarded-Access-Token",
    forwarded_secret_header: str = "X-Ratatoskr-MCP-Forwarding-Secret",
    forwarding_secret: str | None = None,
    allow_remote_sse: bool = False,
    allow_unscoped_sse: bool = False,
    allow_unscoped_stdio: bool = False,
) -> None:
    """Start the MCP server."""
    from app.core.logging_utils import setup_json_logging
    from app.observability.metrics import set_mcp_unscoped_enabled

    setup_json_logging()
    try:
        from app.observability.otel import init_tracing

        init_tracing()
    except Exception:
        pass
    # The matching flush is installed on the SSE app's lifespan; see
    # _install_tracing_flush for why no other seam in this process works.

    if transport == "stdio" and auth_mode != "disabled":
        msg = "HTTP MCP auth modes are only supported with SSE transport."
        raise ValueError(msg)

    app_env = _deployment_env()
    allow_unscoped_production = _env_flag_enabled("MCP_ALLOW_UNSCOPED_PRODUCTION")
    unscoped_sse = transport == "sse" and auth_mode == "disabled" and user_id is None
    unscoped_sse_enabled = unscoped_sse and allow_unscoped_sse
    resolved_host = host
    set_mcp_unscoped_enabled(enabled=unscoped_sse_enabled, app_env=app_env)

    if unscoped_sse and not allow_unscoped_sse:
        msg = (
            "Refusing to start unscoped MCP SSE server. Set MCP_USER_ID/--user-id or "
            "explicitly acknowledge risk via allow_unscoped_sse=True / --allow-unscoped-sse."
        )
        raise ValueError(msg)

    if unscoped_sse_enabled:
        if app_env not in _DEV_ENVS and not allow_unscoped_production:
            logger.error(
                "Refusing unscoped MCP SSE outside a development environment "
                "(app_env=%s, startup_user_scope=all, auth_mode=%s, requested_host=%s, "
                "mcp_allow_unscoped_production=false)",
                app_env,
                auth_mode,
                host,
            )
            msg = (
                "Refusing to start unscoped MCP SSE server outside development "
                "(set APP_ENV to a dev value, scope it with MCP_USER_ID, or set "
                "MCP_ALLOW_UNSCOPED_PRODUCTION=true to acknowledge the risk)."
            )
            raise ValueError(msg)
        if not allow_unscoped_production and not _is_loopback_host(host):
            logger.error(
                "Refusing requested non-loopback bind for unscoped MCP SSE; "
                "binding loopback instead "
                "(app_env=%s, startup_user_scope=all, auth_mode=%s, requested_host=%s, "
                "resolved_host=%s)",
                app_env,
                auth_mode,
                host,
                _UNSCOPED_SSE_LOOPBACK_HOST,
            )
            resolved_host = _UNSCOPED_SSE_LOOPBACK_HOST
        logger.error(
            "MCP unscoped SSE mode enabled "
            "(app_env=%s, startup_user_scope=all, auth_mode=%s, host=%s, "
            "mcp_allow_unscoped_production=%s)",
            app_env,
            auth_mode,
            resolved_host,
            allow_unscoped_production,
        )

    if transport == "sse" and not allow_remote_sse and not _is_loopback_host(resolved_host):
        msg = (
            "Refusing to bind MCP SSE to non-loopback host without explicit opt-in "
            "(set allow_remote_sse=True / --allow-remote-sse)."
        )
        raise ValueError(msg)

    if transport == "stdio" and user_id is None and not allow_unscoped_stdio:
        msg = (
            "Refusing to start unscoped MCP stdio server. Set MCP_USER_ID/--user-id or "
            "explicitly acknowledge risk via allow_unscoped_stdio=True / --allow-unscoped-stdio."
        )
        raise ValueError(msg)

    _DEFAULT_CONTEXT.set_user_scope(user_id)
    if database_dsn is not None:
        _DEFAULT_CONTEXT.init_runtime(database_dsn=database_dsn)
    else:
        _DEFAULT_CONTEXT.init_runtime()
    logger.info(
        "Starting Ratatoskr MCP server (transport=%s, startup_user_scope=%s, host=%s)",
        transport,
        user_id if user_id is not None else "all",
        resolved_host if transport == "sse" else "stdio",
    )

    if auth_mode == "jwt":
        logger.info("Hosted MCP request auth enabled (mode=jwt)")
    elif user_id is None:
        logger.error("MCP startup user scope is disabled; queries can access all users")

    if transport == "sse":
        import uvicorn

        if allow_remote_sse:
            mcp.settings.transport_security = None

        app = _build_sse_app(
            mcp_server=mcp,
            auth_mode=auth_mode,
            forwarded_access_token_header=forwarded_access_token_header,
            forwarded_secret_header=forwarded_secret_header,
            forwarding_secret=forwarding_secret,
        )
        uvicorn.run(app, host=resolved_host, port=port, log_level="info")
    else:
        mcp.run(transport="stdio")
