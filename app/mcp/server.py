"""MCP server entrypoint and FastMCP composition shell."""

from __future__ import annotations

import logging
import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from app.mcp.aggregation_service import AggregationMcpService
from app.mcp.article_service import ArticleReadService
from app.mcp.catalog_service import CatalogReadService
from app.mcp.context import McpServerContext
from app.mcp.http_auth import McpHttpAuthMiddleware
from app.mcp.resource_registrations import register_resources
from app.mcp.semantic_service import SemanticSearchService
from app.mcp.signal_service import SignalMcpService
from app.mcp.tool_registrations import register_tools
from app.mcp.x_search_service import XSearchService

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


def _build_sse_app(
    *,
    mcp_server: FastMCP,
    auth_mode: str,
    forwarded_access_token_header: str,
    forwarded_secret_header: str,
    forwarding_secret: str | None,
) -> Any:
    app: Any = mcp_server.sse_app()
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

    register_tools(
        mcp,
        context=server_context,
        aggregation_service=aggregation_service,
        article_service=article_service,
        catalog_service=catalog_service,
        semantic_service=semantic_service,
        signal_service=signal_service,
        x_search_service_inst=x_search_service_inst,
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
