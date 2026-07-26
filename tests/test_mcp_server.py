from __future__ import annotations

import importlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest


class FakeFastMCP:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.settings = SimpleNamespace(host="127.0.0.1", port=8000, transport_security=None)
        self.registered_tools: list[str] = []
        self.registered_tool_handlers: dict[str, Any] = {}
        self.registered_resources: list[str] = []
        self.registered_resource_handlers: dict[str, Any] = {}
        self.run_calls: list[dict[str, Any]] = []
        self.sse_apps: list[object] = []

    def tool(self, *_args: Any, **_kwargs: Any):
        def decorator(fn):
            self.registered_tools.append(fn.__name__)
            self.registered_tool_handlers[fn.__name__] = fn
            return fn

        return decorator

    def resource(self, uri: str, *_args: Any, **_kwargs: Any):
        def decorator(fn):
            self.registered_resources.append(uri)
            self.registered_resource_handlers[uri] = fn
            return fn

        return decorator

    def run(self, **kwargs: Any) -> None:
        self.run_calls.append(kwargs)

    def sse_app(self):
        app = object()
        self.sse_apps.append(app)
        return app


@dataclass
class FakeTransportSecuritySettings:
    enable_dns_rebinding_protection: bool


def install_fake_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp_module = ModuleType("mcp")
    server_module = ModuleType("mcp.server")
    fastmcp_module = ModuleType("mcp.server.fastmcp")
    transport_module = ModuleType("mcp.server.transport_security")

    fastmcp_any: Any = fastmcp_module
    transport_any: Any = transport_module
    server_any: Any = server_module
    mcp_any: Any = mcp_module

    fastmcp_any.FastMCP = FakeFastMCP
    fastmcp_any.Context = type("FakeContext", (), {})
    transport_any.TransportSecuritySettings = FakeTransportSecuritySettings
    server_any.fastmcp = fastmcp_module
    server_any.transport_security = transport_module
    mcp_any.server = server_module

    monkeypatch.setitem(sys.modules, "mcp", mcp_module)
    monkeypatch.setitem(sys.modules, "mcp.server", server_module)
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp_module)
    monkeypatch.setitem(sys.modules, "mcp.server.transport_security", transport_module)


def load_server_module(monkeypatch: pytest.MonkeyPatch):
    install_fake_mcp(monkeypatch)
    sys.modules.pop("app.mcp.server", None)
    return importlib.import_module("app.mcp.server")


def test_run_server_rejects_insecure_sse(monkeypatch: pytest.MonkeyPatch) -> None:
    server = load_server_module(monkeypatch)
    monkeypatch.setattr(server._DEFAULT_CONTEXT, "init_runtime", lambda _db_path=None: None)

    with pytest.raises(ValueError, match="non-loopback"):
        server.run_server(transport="sse", host="0.0.0.0", user_id=1)

    with pytest.raises(ValueError, match="unscoped"):
        server.run_server(transport="sse", host="127.0.0.1", user_id=None)


def test_run_server_rejects_production_unscoped_sse_without_env_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = load_server_module(monkeypatch)
    init_called = False

    def fake_init_runtime(*_args: Any, **_kwargs: Any) -> None:
        nonlocal init_called
        init_called = True

    monkeypatch.setattr(server._DEFAULT_CONTEXT, "init_runtime", fake_init_runtime)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("MCP_ALLOW_UNSCOPED_PRODUCTION", raising=False)

    with pytest.raises(ValueError, match="MCP_ALLOW_UNSCOPED_PRODUCTION"):
        server.run_server(
            transport="sse",
            host="127.0.0.1",
            user_id=None,
            auth_mode="disabled",
            allow_unscoped_sse=True,
        )

    assert init_called is False


def test_run_server_forces_unscoped_sse_to_loopback_without_env_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = load_server_module(monkeypatch)
    monkeypatch.setattr(server._DEFAULT_CONTEXT, "init_runtime", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("MCP_ALLOW_UNSCOPED_PRODUCTION", raising=False)

    captured: dict[str, Any] = {}

    class FakeUvicorn:
        @staticmethod
        def run(app: Any, host: str, port: int, log_level: str) -> None:
            captured["app"] = app
            captured["host"] = host
            captured["port"] = port
            captured["log_level"] = log_level

    monkeypatch.setitem(sys.modules, "uvicorn", FakeUvicorn)

    server.run_server(
        transport="sse",
        host="0.0.0.0",
        port=8200,
        user_id=None,
        auth_mode="disabled",
        allow_remote_sse=True,
        allow_unscoped_sse=True,
    )

    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8200


def test_run_server_rejects_unscoped_stdio_without_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = load_server_module(monkeypatch)
    monkeypatch.setattr(server._DEFAULT_CONTEXT, "init_runtime", lambda _db_path=None: None)

    with pytest.raises(ValueError, match="unscoped MCP stdio"):
        server.run_server(transport="stdio", user_id=None)


def test_run_server_allows_unscoped_stdio_with_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = load_server_module(monkeypatch)
    monkeypatch.setattr(server._DEFAULT_CONTEXT, "init_runtime", lambda _db_path=None: None)

    server.run_server(transport="stdio", user_id=None, allow_unscoped_stdio=True)

    assert server.mcp.run_calls == [{"transport": "stdio"}]


def test_cli_uses_mcp_env_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    server = load_server_module(monkeypatch)
    import app.cli.mcp_server as mcp_cli

    captured: dict[str, Any] = {}

    def fake_run_server(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setenv("MCP_TRANSPORT", "sse")
    monkeypatch.setenv("MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("MCP_PORT", "9333")
    monkeypatch.setenv("MCP_USER_ID", "4242")
    monkeypatch.setenv("MCP_AUTH_MODE", "jwt")
    monkeypatch.setattr(server, "run_server", fake_run_server)
    monkeypatch.setattr(sys, "argv", ["ratatoskr-mcp-server"])

    mcp_cli.main()

    assert captured["transport"] == "sse"
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9333
    assert captured["user_id"] == 4242
    assert captured["auth_mode"] == "jwt"
    assert captured["database_dsn"] is None
    assert captured["allow_unscoped_stdio"] is False


def test_cli_accepts_postgres_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    server = load_server_module(monkeypatch)
    import app.cli.mcp_server as mcp_cli

    captured: dict[str, Any] = {}

    monkeypatch.setattr(server, "run_server", lambda **kwargs: captured.update(kwargs))
    monkeypatch.setattr(
        sys,
        "argv",
        ["ratatoskr-mcp-server", "--dsn", "postgresql+asyncpg://u:p@localhost:5432/db"],
    )

    mcp_cli.main()

    assert captured["database_dsn"] == "postgresql+asyncpg://u:p@localhost:5432/db"


def test_cli_rejects_legacy_db_path(monkeypatch: pytest.MonkeyPatch) -> None:
    load_server_module(monkeypatch)
    import app.cli.mcp_server as mcp_cli

    monkeypatch.setattr(sys, "argv", ["ratatoskr-mcp-server", "--db-path", "/tmp/app.db"])

    with pytest.raises(SystemExit) as exc_info:
        mcp_cli.main()

    assert exc_info.value.code == 2


def test_run_server_allows_hosted_auth_without_startup_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = load_server_module(monkeypatch)
    monkeypatch.setattr(server._DEFAULT_CONTEXT, "init_runtime", lambda _db_path=None: None)

    captured: dict[str, Any] = {}

    class FakeUvicorn:
        @staticmethod
        def run(app: Any, host: str, port: int, log_level: str) -> None:
            captured["app"] = app
            captured["host"] = host
            captured["port"] = port
            captured["log_level"] = log_level

    monkeypatch.setitem(sys.modules, "uvicorn", FakeUvicorn)

    server.run_server(
        transport="sse",
        host="127.0.0.1",
        port=8200,
        user_id=None,
        auth_mode="jwt",
    )

    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8200
    assert captured["log_level"] == "info"


def test_create_mcp_server_registers_expected_tools_and_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = load_server_module(monkeypatch)
    mcp = server.create_mcp_server()

    assert set(mcp.registered_tools) == {
        "create_aggregation_bundle",
        "get_aggregation_bundle",
        "list_aggregation_bundles",
        "check_source_supported",
        "search_articles",
        "get_article",
        "list_articles",
        "get_article_content",
        "get_stats",
        "find_by_entity",
        "x_search",
        "ask_my_archive",
        "list_collections",
        "get_collection",
        "list_videos",
        "get_video_transcript",
        "check_url",
        "semantic_search",
        "hybrid_search",
        "find_similar_articles",
        "vector_health",
        "vector_index_stats",
        "vector_sync_gap",
        "list_signal_sources",
        "list_user_signals",
        "set_signal_source_active",
        "update_signal_feedback",
        "promote_to_library",
    }
    assert set(mcp.registered_resources) == {
        "ratatoskr://aggregations/recent",
        "ratatoskr://aggregations/{session_id}",
        "ratatoskr://articles/recent",
        "ratatoskr://articles/favorites",
        "ratatoskr://articles/unread",
        "ratatoskr://stats",
        "ratatoskr://tags",
        "ratatoskr://entities",
        "ratatoskr://domains",
        "ratatoskr://collections",
        "ratatoskr://videos/recent",
        "ratatoskr://processing/stats",
        "ratatoskr://vector/health",
        "ratatoskr://vector/index-stats",
        "ratatoskr://vector/sync-gap",
        "ratatoskr://signals/recent",
        "ratatoskr://sources",
    }


def test_server_module_is_thin_shell() -> None:
    server_text = Path("app/mcp/server.py").read_text(encoding="utf-8")
    assert "from app.db.models" not in server_text
    assert "@mcp.tool" not in server_text
    assert "@mcp.resource" not in server_text
    assert "_runtime =" not in server_text
    assert "_scope_user_id =" not in server_text
    assert "_MCP_USER_ID" not in server_text


def test_mcp_tool_contribution_is_schema_testable_and_registers_on_fake_mcp() -> None:
    from app.mcp.tool_registrations import McpToolContribution

    def sample_tool(value: int) -> str:
        """Sample tool."""
        return str(value)

    contribution = McpToolContribution.from_handler(sample_tool)
    assert contribution.model_dump() == {"name": "sample_tool", "description": "Sample tool."}

    mcp = FakeFastMCP()
    contribution.register(mcp)

    assert mcp.registered_tools == ["sample_tool"]
    assert mcp.registered_tool_handlers["sample_tool"] is sample_tool


def test_mcp_resource_contribution_is_schema_testable_and_registers_on_fake_mcp() -> None:
    from app.mcp.resource_registrations import McpResourceContribution

    async def sample_resource() -> str:
        """Sample resource."""
        return "{}"

    contribution = McpResourceContribution.from_handler("ratatoskr://sample", sample_resource)
    assert contribution.model_dump() == {
        "uri": "ratatoskr://sample",
        "name": "sample_resource",
        "description": "Sample resource.",
    }

    mcp = FakeFastMCP()
    contribution.register(mcp)

    assert mcp.registered_resources == ["ratatoskr://sample"]
    assert mcp.registered_resource_handlers["ratatoskr://sample"] is sample_resource


def test_registered_tool_still_records_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.mcp.context import McpServerContext
    from app.mcp.tool_registrations import register_tools

    metric_calls: list[dict[str, Any]] = []

    def fake_record_request(**kwargs: Any) -> None:
        metric_calls.append(kwargs)

    monkeypatch.setattr("app.mcp.tool_registrations.record_request", fake_record_request)

    mcp = FakeFastMCP()
    register_tools(
        mcp,
        context=McpServerContext(),
        aggregation_service=cast(
            "Any",
            SimpleNamespace(check_source_supported=lambda **_kwargs: {"supported": True}),
        ),
        article_service=cast("Any", SimpleNamespace()),
        catalog_service=cast("Any", SimpleNamespace()),
        semantic_service=cast("Any", SimpleNamespace()),
    )

    payload = json.loads(
        mcp.registered_tool_handlers["check_source_supported"]("https://example.com")
    )

    assert payload == {"supported": True}
    assert metric_calls
    assert metric_calls[-1]["request_type"] == "check_source_supported"
    assert metric_calls[-1]["status"] == "success"
    assert metric_calls[-1]["source"] == "mcp"
