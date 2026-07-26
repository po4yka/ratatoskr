from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.mcp.archive_research_service import ArchiveResearchMcpService, _annotation_terms
from app.mcp.context import McpServerContext


def test_annotation_terms_are_bounded_literal_words() -> None:
    assert _annotation_terms("How does checkpoint_durability work? 100%") == [
        "how",
        "does",
        "checkpoint_durability",
        "work",
        "100",
    ]


@pytest.mark.asyncio
async def test_blank_research_query_returns_correlation_bearing_error() -> None:
    service = ArchiveResearchMcpService(
        McpServerContext(user_id=1),
        SimpleNamespace(),
        SimpleNamespace(),
    )

    result = await service.research("   ")

    assert result["correlation_id"]
    assert f"Error ID: {result['correlation_id']}" in result["error"]
