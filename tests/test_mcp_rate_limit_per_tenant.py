"""MCP tool rate limiter is bucketed per (tool, tenant), not per tool alone.

In the hosted multi-tenant JWT mode one process serves many authenticated
users, so a tool-name-only rate-limit bucket would let any single caller
exhaust the shared budget for every other tenant. These tests pin the
per-tenant keying and the identity-key resolution order.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.mcp import tool_registrations as tr
from app.mcp.tool_registrations import _mcp_identity_key, register_tools

pytestmark = pytest.mark.no_network


class RecordingMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *_args: Any, **_kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            self.tools[fn.__name__] = fn
            return fn

        return decorator


def _services() -> tuple[Any, Any, Any, Any]:
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
    return aggregation_service, article_service, catalog_service, semantic_service


def _register_for(user_id: int | None) -> RecordingMCP:
    mcp = RecordingMCP()
    agg, art, cat, sem = _services()
    register_tools(
        mcp,
        context=cast("Any", SimpleNamespace(user_id=user_id, client_id=None)),
        aggregation_service=cast("Any", agg),
        article_service=cast("Any", art),
        catalog_service=cast("Any", cat),
        semantic_service=cast("Any", sem),
    )
    return mcp


async def _run_bundle(mcp: RecordingMCP) -> dict[str, Any]:
    payload = await mcp.tools["create_aggregation_bundle"](
        items=[{"url": "https://example.com/article"}]
    )
    return cast("dict[str, Any]", json.loads(payload))


# The embedding-cost tools each embed their query through the vector/local
# embedding provider on every invocation, so they carry the same tight budget as
# the scrape+LLM tools. ``find_similar_articles`` needs a summary_id; the others
# take a text query.
_EMBEDDING_TOOL_CALLS: dict[str, dict[str, Any]] = {
    "semantic_search": {"description": "an idea"},
    "hybrid_search": {"query": "an idea"},
    "find_similar_articles": {"summary_id": 1},
}


async def _run_tool(mcp: RecordingMCP, name: str, /, **kwargs: Any) -> dict[str, Any]:
    payload = await mcp.tools[name](**kwargs)
    return cast("dict[str, Any]", json.loads(payload))


def test_identity_key_prefers_user_then_client_then_anon() -> None:
    assert _mcp_identity_key(SimpleNamespace(user_id=42, client_id="cid")) == "u42"
    assert _mcp_identity_key(SimpleNamespace(user_id=None, client_id="cid")) == "ccid"
    assert _mcp_identity_key(SimpleNamespace(user_id=None, client_id=None)) == "anon"


@pytest.mark.asyncio
async def test_expensive_tool_budget_is_isolated_per_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tr._MCP_TOOL_RATE_LIMITER.reset()
    # Tighten the expensive-tool budget so the test needs only a few calls.
    monkeypatch.setattr(tr, "_MCP_EXPENSIVE_TOOL_LIMIT", 2)

    tenant_a = _register_for(1)
    tenant_b = _register_for(2)

    # Tenant A spends its full expensive budget.
    assert "error" not in await _run_bundle(tenant_a)
    assert "error" not in await _run_bundle(tenant_a)
    # A's next call is throttled...
    throttled = await _run_bundle(tenant_a)
    assert throttled.get("error") == "rate_limited"

    # ...but tenant B still has an independent, full budget. A tool-name-only
    # bucket would have throttled B here too.
    assert "error" not in await _run_bundle(tenant_b)
    assert "error" not in await _run_bundle(tenant_b)
    throttled_b = await _run_bundle(tenant_b)
    assert throttled_b.get("error") == "rate_limited"

    tr._MCP_TOOL_RATE_LIMITER.reset()


@pytest.mark.parametrize("tool_name", sorted(_EMBEDDING_TOOL_CALLS))
@pytest.mark.asyncio
async def test_embedding_tools_use_expensive_tier(
    tool_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """semantic_search / hybrid_search / find_similar_articles each embed their
    query on every call, so they belong to the tight expensive tier -- not the
    default read tier -- otherwise a caller could drive unbounded billed
    embedding cost (CWE-770). A limit of 1 on the expensive tier and a generous
    default tier proves which bucket each tool lands in.
    """
    tr._MCP_TOOL_RATE_LIMITER.reset()
    monkeypatch.setattr(tr, "_MCP_EXPENSIVE_TOOL_LIMIT", 1)
    monkeypatch.setattr(tr, "_MCP_TOOL_DEFAULT_LIMIT", 50)

    assert tool_name in tr._MCP_EXPENSIVE_TOOLS

    mcp = _register_for(1)
    kwargs = _EMBEDDING_TOOL_CALLS[tool_name]

    # First call consumes the single-slot expensive budget; the second is throttled.
    assert "error" not in await _run_tool(mcp, tool_name, **kwargs)
    throttled = await _run_tool(mcp, tool_name, **kwargs)
    assert throttled.get("error") == "rate_limited"

    # A default-tier read tool is untouched by that budget -- confirms the
    # embedding tools are not merely sharing the (now generous) default bucket.
    assert "error" not in await _run_tool(mcp, "list_articles")
    assert "error" not in await _run_tool(mcp, "list_articles")

    tr._MCP_TOOL_RATE_LIMITER.reset()
