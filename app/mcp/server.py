"""MCP server entrypoint and FastMCP composition shell."""

from __future__ import annotations

import logging
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


def _is_loopback_host(host: str) -> bool:
    return host.strip().lower() in {"127.0.0.1", "::1", "localhost"}


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
        aggregation_service=aggregation_service,
        article_service=article_service,
        catalog_service=catalog_service,
        semantic_service=semantic_service,
        signal_service=signal_service,
        x_search_service_inst=x_search_service_inst,
    )
    register_resources(
        mcp,
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

    setup_json_logging()
    try:
        from app.observability.otel import init_tracing

        init_tracing()
    except Exception:
        pass
    _DEFAULT_CONTEXT.set_user_scope(user_id)
    if database_dsn is not None:
        _DEFAULT_CONTEXT.init_runtime(database_dsn=database_dsn)
    else:
        _DEFAULT_CONTEXT.init_runtime()
    logger.info(
        "Starting Ratatoskr MCP server (transport=%s, startup_user_scope=%s)",
        transport,
        user_id if user_id is not None else "all",
    )

    if transport == "stdio" and auth_mode != "disabled":
        msg = "HTTP MCP auth modes are only supported with SSE transport."
        raise ValueError(msg)

    if transport == "sse" and not allow_remote_sse and not _is_loopback_host(host):
        msg = (
            "Refusing to bind MCP SSE to non-loopback host without explicit opt-in "
            "(set allow_remote_sse=True / --allow-remote-sse)."
        )
        raise ValueError(msg)

    if (
        transport == "sse"
        and auth_mode == "disabled"
        and user_id is None
        and not allow_unscoped_sse
    ):
        msg = (
            "Refusing to start unscoped MCP SSE server. Set MCP_USER_ID/--user-id or "
            "explicitly acknowledge risk via allow_unscoped_sse=True / --allow-unscoped-sse."
        )
        raise ValueError(msg)

    if transport == "stdio" and user_id is None and not allow_unscoped_stdio:
        msg = (
            "Refusing to start unscoped MCP stdio server. Set MCP_USER_ID/--user-id or "
            "explicitly acknowledge risk via allow_unscoped_stdio=True / --allow-unscoped-stdio."
        )
        raise ValueError(msg)

    if auth_mode == "jwt":
        logger.info("Hosted MCP request auth enabled (mode=jwt)")
    elif user_id is None:
        logger.warning("MCP startup user scope is disabled; queries can access all users")

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
        uvicorn.run(app, host=host, port=port, log_level="info")
    else:
        mcp.run(transport="stdio")
