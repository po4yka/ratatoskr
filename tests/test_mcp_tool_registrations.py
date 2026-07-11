from __future__ import annotations

import contextvars
import json
import sys
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.api.routers.auth.tokens import create_access_token
from app.application.dto.aggregation import (
    MultiSourceAggregationOutput,
    MultiSourceExtractionOutput,
    SourceCoverageEntry,
    SourceExtractionItemResult,
)
from app.config import load_config
from app.db.models import User
from app.domain.models.source import SourceKind
from app.mcp.aggregation_service import AggregationMcpService
from app.mcp.context import McpServerContext
from app.mcp.http_auth import McpHttpAuthMiddleware
from app.mcp.tool_registrations import register_tools

pytest_plugins = ("tests.mcp_test_support",)

if TYPE_CHECKING:
    from collections.abc import Callable


class RecordingMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable[..., Any]] = {}

    def tool(self, *_args, **_kwargs):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


def _fake_api_runtime(db: Any) -> SimpleNamespace:
    return SimpleNamespace(
        cfg=load_config(allow_stub_telegram=True),
        db=db,
        background_processor=SimpleNamespace(
            url_processor=SimpleNamespace(content_extractor=MagicMock())
        ),
        core=SimpleNamespace(llm_client=MagicMock()),
    )


@pytest.mark.asyncio
async def test_mcp_tool_registration_records_success_metrics() -> None:
    mcp = RecordingMCP()
    aggregation_service = SimpleNamespace(
        create_aggregation_bundle=AsyncMock(return_value={"session": {"id": 1}}),
        get_aggregation_bundle=AsyncMock(return_value={"session": {"id": 1}}),
        list_aggregation_bundles=AsyncMock(return_value={"sessions": []}),
        check_source_supported=MagicMock(return_value={"supported": True}),
    )
    article_service = SimpleNamespace(
        search_articles=AsyncMock(return_value={"items": []}),
        get_article=AsyncMock(return_value={"id": 1}),
        list_articles=AsyncMock(return_value={"items": []}),
        get_article_content=AsyncMock(return_value={"content": "body"}),
        get_stats=AsyncMock(return_value={"total": 1}),
        find_by_entity=AsyncMock(return_value={"items": []}),
        check_url=AsyncMock(return_value={"duplicate": False}),
    )
    catalog_service = SimpleNamespace(
        list_collections=AsyncMock(return_value={"items": []}),
        get_collection=AsyncMock(return_value={"id": 1}),
        list_videos=AsyncMock(return_value={"items": []}),
        get_video_transcript=AsyncMock(return_value={"video_id": "abc"}),
    )
    semantic_service = SimpleNamespace(
        semantic_search=AsyncMock(return_value={"items": []}),
        hybrid_search=AsyncMock(return_value={"items": []}),
        find_similar_articles=AsyncMock(return_value={"items": []}),
        vector_health=AsyncMock(return_value={"status": "ok"}),
        vector_index_stats=AsyncMock(return_value={"coverage": 1.0}),
        vector_sync_gap=AsyncMock(return_value={"gap": 0}),
    )
    signal_service = SimpleNamespace(
        list_sources=AsyncMock(return_value={"sources": []}),
        list_signals=AsyncMock(return_value={"signals": []}),
        update_signal_feedback=AsyncMock(return_value={"updated": True}),
        set_source_active=AsyncMock(return_value={"updated": True}),
    )
    archive_research_service = SimpleNamespace(
        research=AsyncMock(return_value={"answer": "Evidence [summary:1]", "citations": []})
    )

    register_tools(
        mcp,
        context=McpServerContext(user_id=None),
        aggregation_service=cast("Any", aggregation_service),
        article_service=cast("Any", article_service),
        catalog_service=cast("Any", catalog_service),
        semantic_service=cast("Any", semantic_service),
        signal_service=cast("Any", signal_service),
        archive_research_service=cast("Any", archive_research_service),
    )

    assert len(mcp.tools) == 27
    assert {
        "list_signal_sources",
        "list_user_signals",
        "update_signal_feedback",
        "set_signal_source_active",
        "x_search",
        "ask_my_archive",
    } <= set(mcp.tools)

    with patch("app.mcp.tool_registrations.record_request") as metrics_mock:
        payload = await mcp.tools["create_aggregation_bundle"](
            items=[{"type": "url", "url": "https://example.com"}]
        )

    assert json.loads(payload)["session"]["id"] == 1
    metrics_mock.assert_called_once()
    metric_kwargs = metrics_mock.call_args.kwargs
    assert metric_kwargs["request_type"] == "create_aggregation_bundle"
    assert metric_kwargs["status"] == "success"
    assert metric_kwargs["source"] == "mcp"
    assert metric_kwargs["latency_seconds"] >= 0

    payload = await mcp.tools["ask_my_archive"]("What did I save?")
    assert json.loads(payload)["answer"] == "Evidence [summary:1]"
    archive_research_service.research.assert_awaited_once_with("What did I save?", 12)


@pytest.mark.asyncio
async def test_mcp_tool_registration_records_error_metrics_for_service_errors() -> None:
    mcp = RecordingMCP()
    aggregation_service = SimpleNamespace(
        create_aggregation_bundle=AsyncMock(return_value={"error": "Access denied"}),
        get_aggregation_bundle=AsyncMock(return_value={"error": "Access denied"}),
        list_aggregation_bundles=AsyncMock(return_value={"sessions": []}),
        check_source_supported=MagicMock(return_value={"supported": True}),
    )
    article_service = SimpleNamespace(
        search_articles=AsyncMock(side_effect=RuntimeError("boom")),
        get_article=AsyncMock(return_value={"id": 1}),
        list_articles=AsyncMock(return_value={"items": []}),
        get_article_content=AsyncMock(return_value={"content": "body"}),
        get_stats=AsyncMock(return_value={"total": 1}),
        find_by_entity=AsyncMock(return_value={"items": []}),
        check_url=AsyncMock(return_value={"duplicate": False}),
    )
    catalog_service = SimpleNamespace(
        list_collections=AsyncMock(return_value={"items": []}),
        get_collection=AsyncMock(return_value={"id": 1}),
        list_videos=AsyncMock(return_value={"items": []}),
        get_video_transcript=AsyncMock(return_value={"video_id": "abc"}),
    )
    semantic_service = SimpleNamespace(
        semantic_search=AsyncMock(return_value={"items": []}),
        hybrid_search=AsyncMock(return_value={"items": []}),
        find_similar_articles=AsyncMock(return_value={"items": []}),
        vector_health=AsyncMock(return_value={"status": "ok"}),
        vector_index_stats=AsyncMock(return_value={"coverage": 1.0}),
        vector_sync_gap=AsyncMock(return_value={"gap": 0}),
    )

    register_tools(
        mcp,
        context=McpServerContext(user_id=None),
        aggregation_service=cast("Any", aggregation_service),
        article_service=cast("Any", article_service),
        catalog_service=cast("Any", catalog_service),
        semantic_service=cast("Any", semantic_service),
    )

    with patch("app.mcp.tool_registrations.record_request") as metrics_mock:
        payload = await mcp.tools["create_aggregation_bundle"](
            items=[{"type": "url", "url": "https://example.com"}]
        )

    assert json.loads(payload)["error"] == "Access denied"
    metric_kwargs = metrics_mock.call_args.kwargs
    assert metric_kwargs["request_type"] == "create_aggregation_bundle"
    assert metric_kwargs["status"] == "error"
    assert metric_kwargs["source"] == "mcp"
    assert metric_kwargs["latency_seconds"] >= 0

    with patch("app.mcp.tool_registrations.record_request") as metrics_mock:
        with pytest.raises(RuntimeError, match="boom"):
            await mcp.tools["search_articles"]("query", 5)

    metric_kwargs = metrics_mock.call_args.kwargs
    assert metric_kwargs["request_type"] == "search_articles"
    assert metric_kwargs["status"] == "error"
    assert metric_kwargs["source"] == "mcp"
    assert metric_kwargs["latency_seconds"] >= 0


def test_hosted_mcp_tool_uses_request_scoped_identity_and_client_id(
    mcp_test_db,
    monkeypatch,
) -> None:
    from app.application.services.multi_source_aggregation_service import (
        MultiSourceAggregationRunResult,
    )

    user_id = 4201
    User.create(telegram_user_id=user_id, username="tool-user", is_owner=False)  # type: ignore[attr-defined]

    fake_result = MultiSourceAggregationRunResult(
        extraction=MultiSourceExtractionOutput(
            session_id=501,
            correlation_id="cid-hosted-tool",
            status="completed",
            successful_count=1,
            failed_count=0,
            duplicate_count=0,
            items=[
                SourceExtractionItemResult(
                    position=0,
                    item_id=6001,
                    source_item_id="src_tool_1",
                    source_kind=SourceKind.WEB_ARTICLE,
                    status="extracted",
                    request_id=7001,
                )
            ],
        ),
        aggregation=MultiSourceAggregationOutput(
            session_id=501,
            correlation_id="cid-hosted-tool",
            status="completed",
            source_type="web_article",
            total_items=1,
            extracted_items=1,
            used_source_count=1,
            overview="Hosted MCP tool output",
            source_coverage=[
                SourceCoverageEntry(
                    position=0,
                    item_id=6001,
                    source_item_id="src_tool_1",
                    source_kind=SourceKind.WEB_ARTICLE,
                    status="extracted",
                    used_in_summary=True,
                )
            ],
        ),
    )

    context = McpServerContext(user_id=None)
    context.ensure_api_runtime = AsyncMock(return_value=_fake_api_runtime(mcp_test_db))  # type: ignore[method-assign]
    aggregation_service = AggregationMcpService(context)
    mcp = RecordingMCP()

    article_service = SimpleNamespace(
        search_articles=AsyncMock(return_value={"items": []}),
        get_article=AsyncMock(return_value={"id": 1}),
        list_articles=AsyncMock(return_value={"items": []}),
        get_article_content=AsyncMock(return_value={"content": "body"}),
        get_stats=AsyncMock(return_value={"total": 1}),
        find_by_entity=AsyncMock(return_value={"items": []}),
        check_url=AsyncMock(return_value={"duplicate": False}),
    )
    catalog_service = SimpleNamespace(
        list_collections=AsyncMock(return_value={"items": []}),
        get_collection=AsyncMock(return_value={"id": 1}),
        list_videos=AsyncMock(return_value={"items": []}),
        get_video_transcript=AsyncMock(return_value={"video_id": "abc"}),
    )
    semantic_service = SimpleNamespace(
        semantic_search=AsyncMock(return_value={"items": []}),
        hybrid_search=AsyncMock(return_value={"items": []}),
        find_similar_articles=AsyncMock(return_value={"items": []}),
        vector_health=AsyncMock(return_value={"status": "ok"}),
        vector_index_stats=AsyncMock(return_value={"coverage": 1.0}),
        vector_sync_gap=AsyncMock(return_value={"gap": 0}),
    )

    register_tools(
        mcp,
        context=context,
        aggregation_service=cast("Any", aggregation_service),
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

    aggregate_mock = AsyncMock(return_value=fake_result)

    async def create_tool(request):
        token = request_ctx.set(SimpleNamespace(request=request))
        try:
            payload = await mcp.tools["create_aggregation_bundle"](
                items=[{"url": "https://example.com/article"}],
                lang_preference="en",
                metadata={"submitted_by": "hosted-tool-test"},
            )
        finally:
            request_ctx.reset(token)
        return JSONResponse(json.loads(payload))

    token = create_access_token(user_id=user_id, client_id="mcp-public-v1", username="tool-user")
    app = Starlette(routes=[Route("/tool", create_tool)])
    app_asgi: Any = McpHttpAuthMiddleware(
        app,
        forwarded_access_token_header="X-Ratatoskr-Forwarded-Access-Token",
        forwarded_secret_header="X-Ratatoskr-MCP-Forwarding-Secret",
        forwarding_secret=None,
    )

    with patch(
        "app.application.services.multi_source_aggregation_service.MultiSourceAggregationService.aggregate",
        new=aggregate_mock,
    ):
        with TestClient(app_asgi) as client:
            response = client.get("/tool", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["session"]["id"] == 501
    aggregate_kwargs = aggregate_mock.await_args.kwargs
    assert aggregate_kwargs["user_id"] == user_id
    assert aggregate_kwargs["metadata"]["client_id"] == "mcp-public-v1"
    assert aggregate_kwargs["metadata"]["submitted_by"] == "hosted-tool-test"
