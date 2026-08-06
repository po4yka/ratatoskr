from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.application.dto.aggregation import (
    MultiSourceAggregationOutput,
    MultiSourceExtractionOutput,
    SourceCoverageEntry,
    SourceExtractionItemResult,
)
from app.application.services.multi_source_aggregation_service import (
    MultiSourceAggregationRunResult,
)
from app.config import load_config
from app.di.repositories import build_aggregation_session_repository
from app.domain.models.source import SourceKind
from app.mcp.aggregation_service import AggregationMcpService
from app.mcp.context import McpServerContext
from app.mcp.http_auth import McpRequestIdentity
from tests.mcp_test_support import create_mcp_user

pytest_plugins = ("tests.mcp_test_support",)


def _fake_api_runtime(db) -> SimpleNamespace:
    return SimpleNamespace(
        cfg=load_config(allow_stub_telegram=True),
        db=db,
        background_processor=SimpleNamespace(
            url_processor=SimpleNamespace(content_extractor=MagicMock())
        ),
        core=SimpleNamespace(llm_client=MagicMock(), db=db),
    )


@pytest.mark.asyncio
async def test_list_aggregation_bundles_is_scoped_to_user(mcp_test_db) -> None:
    user_id = 1001
    other_user_id = 1002
    await create_mcp_user(mcp_test_db, telegram_user_id=user_id, username="mcp-user")
    await create_mcp_user(mcp_test_db, telegram_user_id=other_user_id, username="other-user")

    repo = build_aggregation_session_repository(mcp_test_db)
    visible_session_id = await repo.async_create_aggregation_session(
        user_id=user_id,
        correlation_id="cid-visible",
        total_items=1,
        bundle_metadata={"entrypoint": "mcp"},
    )
    await repo.async_create_aggregation_session(
        user_id=other_user_id,
        correlation_id="cid-hidden",
        total_items=1,
        bundle_metadata={"entrypoint": "mcp"},
    )

    context = McpServerContext(user_id=user_id)
    context.ensure_api_runtime = AsyncMock(return_value=_fake_api_runtime(mcp_test_db))  # type: ignore[method-assign]
    service = AggregationMcpService(context)

    payload = await service.list_aggregation_bundles(limit=20, offset=0)

    assert payload["total"] == 1
    assert [session["id"] for session in payload["sessions"]] == [visible_session_id]


@pytest.mark.asyncio
async def test_get_aggregation_bundle_rejects_foreign_session(mcp_test_db) -> None:
    user_id = 2001
    other_user_id = 2002
    await create_mcp_user(mcp_test_db, telegram_user_id=user_id, username="mcp-user")
    await create_mcp_user(mcp_test_db, telegram_user_id=other_user_id, username="other-user")

    repo = build_aggregation_session_repository(mcp_test_db)
    foreign_session_id = await repo.async_create_aggregation_session(
        user_id=other_user_id,
        correlation_id="cid-foreign",
        total_items=1,
        bundle_metadata={"entrypoint": "mcp"},
    )

    context = McpServerContext(user_id=user_id)
    context.ensure_api_runtime = AsyncMock(return_value=_fake_api_runtime(mcp_test_db))  # type: ignore[method-assign]
    service = AggregationMcpService(context)

    payload = await service.get_aggregation_bundle(foreign_session_id)

    assert payload == {"error": "Access denied"}


@pytest.mark.asyncio
async def test_create_aggregation_bundle_returns_workflow_payload(mcp_test_db) -> None:
    user_id = 3001
    await create_mcp_user(mcp_test_db, telegram_user_id=user_id, username="mcp-user")

    fake_result = MultiSourceAggregationRunResult(
        extraction=MultiSourceExtractionOutput(
            session_id=91,
            correlation_id="cid-mcp-agg-create",
            status="completed",
            successful_count=1,
            failed_count=0,
            duplicate_count=0,
            items=[
                SourceExtractionItemResult(
                    position=0,
                    item_id=4001,
                    source_item_id="src_mcp_a",
                    source_kind=SourceKind.WEB_ARTICLE,
                    status="extracted",
                    request_id=5001,
                )
            ],
        ),
        aggregation=MultiSourceAggregationOutput(
            session_id=91,
            correlation_id="cid-mcp-agg-create",
            status="completed",
            source_type="web_article",
            total_items=1,
            extracted_items=1,
            used_source_count=1,
            overview="MCP synthesis output",
            source_coverage=[
                SourceCoverageEntry(
                    position=0,
                    item_id=4001,
                    source_item_id="src_mcp_a",
                    source_kind=SourceKind.WEB_ARTICLE,
                    status="extracted",
                    used_in_summary=True,
                )
            ],
        ),
    )

    context = McpServerContext(user_id=user_id)
    context.ensure_api_runtime = AsyncMock(return_value=_fake_api_runtime(mcp_test_db))  # type: ignore[method-assign]
    service = AggregationMcpService(context)

    with patch(
        "app.application.services.multi_source_aggregation_service.MultiSourceAggregationService.aggregate",
        new=AsyncMock(return_value=fake_result),
    ):
        payload = await service.create_aggregation_bundle(
            items=[{"url": "https://example.com/article"}],
            lang_preference="en",
            metadata={"entrypoint": "test"},
        )

    assert payload["session"]["id"] == 91
    assert payload["session"]["source_type"] == "web_article"
    assert payload["aggregation"]["overview"] == "MCP synthesis output"
    assert payload["items"][0]["source_kind"] == "web_article"


@pytest.mark.asyncio
async def test_create_aggregation_bundle_uses_request_scoped_identity_and_client_id(
    mcp_test_db,
) -> None:
    user_id = 3101
    await create_mcp_user(mcp_test_db, telegram_user_id=user_id, username="mcp-user")

    fake_result = MultiSourceAggregationRunResult(
        extraction=MultiSourceExtractionOutput(
            session_id=92,
            correlation_id="cid-mcp-agg-auth",
            status="completed",
            successful_count=1,
            failed_count=0,
            duplicate_count=0,
            items=[
                SourceExtractionItemResult(
                    position=0,
                    item_id=4002,
                    source_item_id="src_mcp_b",
                    source_kind=SourceKind.WEB_ARTICLE,
                    status="extracted",
                    request_id=5002,
                )
            ],
        ),
        aggregation=MultiSourceAggregationOutput(
            session_id=92,
            correlation_id="cid-mcp-agg-auth",
            status="completed",
            source_type="web_article",
            total_items=1,
            extracted_items=1,
            used_source_count=1,
            overview="Scoped MCP synthesis output",
            source_coverage=[
                SourceCoverageEntry(
                    position=0,
                    item_id=4002,
                    source_item_id="src_mcp_b",
                    source_kind=SourceKind.WEB_ARTICLE,
                    status="extracted",
                    used_in_summary=True,
                )
            ],
        ),
    )

    context = McpServerContext(user_id=9999)
    context.ensure_api_runtime = AsyncMock(return_value=_fake_api_runtime(mcp_test_db))  # type: ignore[method-assign]
    service = AggregationMcpService(context)
    aggregate_mock = AsyncMock(return_value=fake_result)

    with (
        patch(
            "app.application.services.multi_source_aggregation_service.MultiSourceAggregationService.aggregate",
            new=aggregate_mock,
        ),
        context.request_identity_scope(
            McpRequestIdentity(
                user_id=user_id,
                client_id="mcp-public-v1",
                username="scoped-user",
                auth_source="authorization",
            )
        ),
    ):
        payload = await service.create_aggregation_bundle(
            items=[{"url": "https://example.com/article"}],
            lang_preference="en",
            metadata={"submitted_by": "request-scope"},
        )

    assert payload["session"]["id"] == 92
    aggregate_kwargs = aggregate_mock.await_args.kwargs
    assert aggregate_kwargs["user_id"] == user_id
    assert aggregate_kwargs["metadata"]["entrypoint"] == "mcp"
    assert aggregate_kwargs["metadata"]["client_id"] == "mcp-public-v1"
    assert aggregate_kwargs["metadata"]["submitted_by"] == "request-scope"


def test_check_source_supported_classifies_url() -> None:
    service = AggregationMcpService(McpServerContext())

    payload = service.check_source_supported("https://x.com/example/status/123")

    assert payload["supported"] is True
    assert payload["source_kind"] == "x_post"
