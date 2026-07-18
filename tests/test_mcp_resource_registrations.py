from __future__ import annotations

import asyncio
import contextvars
import json
import sys
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.api.routers.auth.tokens import create_access_token
from app.config import load_config
from app.db.models import User
from app.di.repositories import build_aggregation_session_repository
from app.mcp.aggregation_service import AggregationMcpService
from app.mcp.context import McpServerContext
from app.mcp.http_auth import McpHttpAuthMiddleware
from app.mcp.resource_registrations import register_resources

pytest_plugins = ("tests.mcp_test_support",)

if TYPE_CHECKING:
    from collections.abc import Callable


class RecordingMCP:
    def __init__(self) -> None:
        self.resources: dict[str, Callable[..., Any]] = {}
        self.resource_uris: list[str] = []

    def resource(self, uri: str, *_args: Any, **_kwargs: Any):
        def decorator(fn):
            self.resources[fn.__name__] = fn
            self.resource_uris.append(uri)
            return fn

        return decorator


def _fake_api_runtime(db) -> SimpleNamespace:
    return SimpleNamespace(
        cfg=load_config(allow_stub_telegram=True),
        db=db,
        background_processor=SimpleNamespace(
            url_processor=SimpleNamespace(content_extractor=MagicMock())
        ),
        core=SimpleNamespace(llm_client=MagicMock()),
    )


@pytest.mark.asyncio
async def test_aggregation_detail_resource_returns_session_payload() -> None:
    mcp = RecordingMCP()
    aggregation_service = SimpleNamespace(
        list_aggregation_bundles=AsyncMock(return_value={"sessions": []}),
        get_aggregation_bundle=AsyncMock(return_value={"session": {"id": 42}}),
    )
    article_service = SimpleNamespace(
        list_articles=AsyncMock(return_value={"items": []}),
        unread_articles=AsyncMock(return_value={"items": []}),
        get_stats=AsyncMock(return_value={"total": 1}),
        tag_counts=AsyncMock(return_value={"items": []}),
        entity_counts=AsyncMock(return_value={"items": []}),
        domain_counts=AsyncMock(return_value={"items": []}),
    )
    catalog_service = SimpleNamespace(
        list_collections=AsyncMock(return_value={"items": []}),
        list_videos=AsyncMock(return_value={"items": []}),
        processing_stats=AsyncMock(return_value={"jobs": 0}),
    )
    semantic_service = SimpleNamespace(
        vector_health=AsyncMock(return_value={"status": "ok"}),
        vector_index_stats=AsyncMock(return_value={"coverage": 1.0}),
        vector_sync_gap=AsyncMock(return_value={"gap": 0}),
    )
    signal_service = SimpleNamespace(
        list_sources=AsyncMock(return_value={"sources": []}),
        list_signals=AsyncMock(return_value={"signals": []}),
    )

    register_resources(
        mcp,
        context=cast("Any", SimpleNamespace(user_id=1, client_id=None)),
        aggregation_service=cast("Any", aggregation_service),
        article_service=cast("Any", article_service),
        catalog_service=cast("Any", catalog_service),
        semantic_service=cast("Any", semantic_service),
        signal_service=cast("Any", signal_service),
    )

    payload = await mcp.resources["aggregation_bundle_resource"]("42")

    assert json.loads(payload)["session"]["id"] == 42
    assert len(mcp.resource_uris) == 17
    assert "ratatoskr://aggregations/{session_id}" in mcp.resource_uris
    assert "ratatoskr://signals/recent" in mcp.resource_uris
    assert "ratatoskr://sources" in mcp.resource_uris


def test_hosted_mcp_resource_uses_request_scoped_identity(mcp_test_db, monkeypatch) -> None:
    user_id = 4101
    User.create(telegram_user_id=user_id, username="resource-user", is_owner=False)  # type: ignore[attr-defined]

    repo = build_aggregation_session_repository(mcp_test_db)
    session_id = asyncio.run(
        repo.async_create_aggregation_session(
            user_id=user_id,
            correlation_id="cid-resource-scope",
            total_items=1,
            bundle_metadata={"entrypoint": "mcp"},
        )
    )

    context = McpServerContext(user_id=None)
    context.ensure_api_runtime = AsyncMock(return_value=_fake_api_runtime(mcp_test_db))  # type: ignore[method-assign]
    aggregation_service = AggregationMcpService(context)
    mcp = RecordingMCP()

    article_service = SimpleNamespace(
        list_articles=AsyncMock(return_value={"items": []}),
        unread_articles=AsyncMock(return_value={"items": []}),
        get_stats=AsyncMock(return_value={"total": 1}),
        tag_counts=AsyncMock(return_value={"items": []}),
        entity_counts=AsyncMock(return_value={"items": []}),
        domain_counts=AsyncMock(return_value={"items": []}),
    )
    catalog_service = SimpleNamespace(
        list_collections=AsyncMock(return_value={"items": []}),
        list_videos=AsyncMock(return_value={"items": []}),
        processing_stats=AsyncMock(return_value={"jobs": 0}),
    )
    semantic_service = SimpleNamespace(
        vector_health=AsyncMock(return_value={"status": "ok"}),
        vector_index_stats=AsyncMock(return_value={"coverage": 1.0}),
        vector_sync_gap=AsyncMock(return_value={"gap": 0}),
    )

    register_resources(
        mcp,
        context=context,
        aggregation_service=aggregation_service,
        article_service=cast("Any", article_service),
        catalog_service=cast("Any", catalog_service),
        semantic_service=cast("Any", semantic_service),
    )

    lowlevel_module = ModuleType("mcp.server.lowlevel.server")
    request_ctx: contextvars.ContextVar[object] = contextvars.ContextVar("request_ctx")
    lowlevel_any: Any = lowlevel_module
    lowlevel_any.request_ctx = request_ctx
    monkeypatch.setitem(sys.modules, "mcp.server.lowlevel.server", lowlevel_module)
    monkeypatch.setenv("ALLOWED_USER_IDS", str(user_id))
    monkeypatch.setenv("ALLOWED_CLIENT_IDS", "mcp-public-v1")

    async def bundle_resource(request):
        token = request_ctx.set(SimpleNamespace(request=request))
        try:
            payload = await mcp.resources["aggregation_bundle_resource"](str(session_id))
        finally:
            request_ctx.reset(token)
        return JSONResponse(json.loads(payload))

    token = create_access_token(
        user_id=user_id, client_id="mcp-public-v1", username="resource-user"
    )
    app = Starlette(routes=[Route("/resource", bundle_resource)])
    app_asgi = McpHttpAuthMiddleware(
        app,
        forwarded_access_token_header="X-Ratatoskr-Forwarded-Access-Token",
        forwarded_secret_header="X-Ratatoskr-MCP-Forwarding-Secret",
        forwarding_secret=None,
    )

    with TestClient(app_asgi) as client:
        response = client.get("/resource", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["session"]["id"] == session_id
    assert payload["session"]["user"] == user_id


def _rate_limit_services() -> dict[str, Any]:
    return {
        "aggregation_service": SimpleNamespace(
            list_aggregation_bundles=AsyncMock(return_value={"sessions": []}),
            get_aggregation_bundle=AsyncMock(return_value={"session": {"id": 1}}),
        ),
        "article_service": SimpleNamespace(
            list_articles=AsyncMock(return_value={"items": []}),
            unread_articles=AsyncMock(return_value={"items": []}),
            get_stats=AsyncMock(return_value={"total": 1}),
            tag_counts=AsyncMock(return_value={"items": []}),
            entity_counts=AsyncMock(return_value={"items": []}),
            domain_counts=AsyncMock(return_value={"items": []}),
        ),
        "catalog_service": SimpleNamespace(
            list_collections=AsyncMock(return_value={"items": []}),
            list_videos=AsyncMock(return_value={"items": []}),
            processing_stats=AsyncMock(return_value={"jobs": 0}),
        ),
        "semantic_service": SimpleNamespace(
            vector_health=AsyncMock(return_value={"status": "ok"}),
            vector_index_stats=AsyncMock(return_value={"coverage": 1.0}),
            vector_sync_gap=AsyncMock(return_value={"gap": 0}),
        ),
        "signal_service": SimpleNamespace(
            list_sources=AsyncMock(return_value={"sources": []}),
            list_signals=AsyncMock(return_value={"signals": []}),
        ),
    }


@pytest.mark.asyncio
async def test_resource_reads_are_rate_limited_per_tenant(monkeypatch) -> None:
    """The 17 resources route through the SAME tool-layer limiter, bucketed per
    tenant -- previously they bypassed rate limiting entirely (CWE-770)."""
    from app.mcp import tool_registrations as tr

    tr._MCP_TOOL_RATE_LIMITER.reset()
    monkeypatch.setattr(tr, "_MCP_TOOL_DEFAULT_LIMIT", 2)

    def _register(user_id: int) -> RecordingMCP:
        mcp = RecordingMCP()
        register_resources(
            mcp,
            context=cast("Any", SimpleNamespace(user_id=user_id, client_id=None)),
            **{k: cast("Any", v) for k, v in _rate_limit_services().items()},
        )
        return mcp

    async def _read(mcp: RecordingMCP) -> dict[str, Any]:
        return cast("dict[str, Any]", json.loads(await mcp.resources["recent_articles_resource"]()))

    tenant_a = _register(1)
    tenant_b = _register(2)

    # Tenant A spends its (tightened) budget, then is throttled.
    assert "error" not in await _read(tenant_a)
    assert "error" not in await _read(tenant_a)
    assert (await _read(tenant_a)).get("error") == "rate_limited"

    # Tenant B has an independent budget -- a global (per-resource-only) bucket
    # would have throttled B here too.
    assert "error" not in await _read(tenant_b)
    assert "error" not in await _read(tenant_b)
    assert (await _read(tenant_b)).get("error") == "rate_limited"

    tr._MCP_TOOL_RATE_LIMITER.reset()


@pytest.mark.asyncio
async def test_resource_rate_limit_bucket_is_per_operation(monkeypatch) -> None:
    """Each resource keeps its own budget (mirrors per-tool bucketing): exhausting
    one resource does not throttle a different one for the same tenant."""
    from app.mcp import tool_registrations as tr

    tr._MCP_TOOL_RATE_LIMITER.reset()
    monkeypatch.setattr(tr, "_MCP_TOOL_DEFAULT_LIMIT", 1)

    mcp = RecordingMCP()
    register_resources(
        mcp,
        context=cast("Any", SimpleNamespace(user_id=7, client_id=None)),
        **{k: cast("Any", v) for k, v in _rate_limit_services().items()},
    )

    async def _read(name: str) -> dict[str, Any]:
        return cast("dict[str, Any]", json.loads(await mcp.resources[name]()))

    assert "error" not in await _read("recent_articles_resource")
    assert (await _read("recent_articles_resource")).get("error") == "rate_limited"
    # A different resource for the same tenant still has its own fresh budget.
    assert "error" not in await _read("stats_resource")

    tr._MCP_TOOL_RATE_LIMITER.reset()
