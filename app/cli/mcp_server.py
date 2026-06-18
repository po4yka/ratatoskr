"""CLI entry point for the Ratatoskr MCP server.

Starts an MCP (Model Context Protocol) server that exposes articles
and search functionality to local or otherwise trusted AI agents.

Usage:
    # stdio transport (default — for Claude Desktop and other local MCP clients)
    python -m app.cli.mcp_server

    # SSE transport (trusted/local by default, requires startup user scope)
    python -m app.cli.mcp_server --transport sse --user-id 12345

    # Hosted/public SSE mode with JWT auth
    python -m app.cli.mcp_server --transport sse --auth-mode jwt --allow-remote-sse

    # Custom PostgreSQL DSN
    python -m app.cli.mcp_server --dsn postgresql+asyncpg://user:pass@host:5432/db
"""

from __future__ import annotations

import argparse
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="ratatoskr-mcp-server",
        description="Ratatoskr MCP server for local or trusted AI agent integrations",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default=None,
        help="Transport protocol (defaults to MCP_TRANSPORT or 'stdio')",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Bind address for SSE transport (defaults to MCP_HOST or 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port for SSE transport (defaults to MCP_PORT or 8200)",
    )
    parser.add_argument(
        "--dsn",
        default=None,
        help="Override PostgreSQL DSN (defaults to DATABASE_URL)",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Logging level (default: INFO)",
    )
    parser.add_argument(
        "--user-id",
        type=int,
        default=None,
        help="Set startup MCP user scope for local/trusted mode (or use MCP_USER_ID)",
    )
    parser.add_argument(
        "--auth-mode",
        choices=["disabled", "jwt"],
        default=None,
        help="Hosted request auth mode for SSE transport (defaults to MCP_AUTH_MODE)",
    )
    parser.add_argument(
        "--allow-remote-sse",
        action="store_true",
        help="Allow SSE bind on non-loopback hosts (unsafe by default)",
    )
    parser.add_argument(
        "--allow-unscoped-sse",
        action="store_true",
        help="Allow SSE without --user-id / MCP_USER_ID (unsafe by default)",
    )
    parser.add_argument(
        "--allow-unscoped-stdio",
        action="store_true",
        help="Allow stdio without --user-id / MCP_USER_ID (unsafe by default)",
    )

    args = parser.parse_args()
    if args.db_path is not None:
        parser.error("--db-path is no longer supported; set DATABASE_URL or use --dsn DSN")

    import logging

    from app.config.integrations import McpConfig

    cfg = McpConfig.model_validate(dict(os.environ))
    transport = args.transport or cfg.transport
    host = args.host or cfg.host
    port = args.port if args.port is not None else cfg.port
    user_id = args.user_id if args.user_id is not None else cfg.user_id
    auth_mode = args.auth_mode or cfg.auth_mode
    allow_remote_sse = args.allow_remote_sse or cfg.allow_remote_sse
    allow_unscoped_sse = args.allow_unscoped_sse or cfg.allow_unscoped_sse
    allow_unscoped_stdio = args.allow_unscoped_stdio or cfg.allow_unscoped_stdio

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    from app.mcp.server import run_server

    run_server(
        transport=transport,
        host=host,
        port=port,
        database_dsn=args.dsn,
        user_id=user_id,
        auth_mode=auth_mode,
        forwarded_access_token_header=cfg.forwarded_access_token_header,
        forwarded_secret_header=cfg.forwarded_secret_header,
        forwarding_secret=cfg.forwarding_secret,
        allow_remote_sse=allow_remote_sse,
        allow_unscoped_sse=allow_unscoped_sse,
        allow_unscoped_stdio=allow_unscoped_stdio,
    )


if __name__ == "__main__":
    main()
