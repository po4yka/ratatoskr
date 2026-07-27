"""Regression coverage for durable Reader source content across DB sessions."""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete

import app.application.graphs.summarize.graph as graph_mod
from app.adapters.content.extraction_adapter import ContentExtractionAdapter
from app.api.routers.content.summaries import get_summary_content
from app.application.graphs.summarize.deps import SummarizeDeps
from app.application.graphs.summarize.graph import (
    build_summarize_graph,
    run_summarize_graph,
)
from app.application.use_cases.summary_read_model import SummaryReadModelUseCase
from app.config.database import DatabaseConfig
from app.db.models import CrawlResult, LLMCall, Request, Summary
from app.db.session import Database
from app.domain.models.request import RequestStatus
from app.infrastructure.persistence.repositories.crawl_result_repository import (
    CrawlResultRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.llm_repository import LLMRepositoryAdapter
from app.infrastructure.persistence.repositories.request_repository import (
    RequestRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.summary_repository import (
    SummaryRepositoryAdapter,
)

pytestmark = pytest.mark.integration

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


_SOURCE_TEXT = "The normalized article survives a completely fresh database session."


class _PersistingContentExtractor:
    """Minimal real persistence seam used by the compiled extraction node."""

    def __init__(self, crawls: CrawlResultRepositoryAdapter) -> None:
        self._crawls = crawls

    async def extract_content_pure(
        self,
        url: str,
        correlation_id: str | None = None,
        request_id: int | None = None,
    ) -> tuple[str, str, dict[str, object]]:
        assert request_id is not None
        artifact_id = await self._crawls.async_upsert_crawl_result(
            request_id=request_id,
            success=True,
            content_text=_SOURCE_TEXT,
            markdown="# Provider-specific raw payload",
            source_url=url,
            status="ok",
            endpoint="integration-test",
            correlation_id=correlation_id,
            winning_provider="integration-test",
        )
        return (
            _SOURCE_TEXT,
            "integration-test",
            {
                "artifact_id": artifact_id,
                "detected_lang": "en",
                "content_length": len(_SOURCE_TEXT),
            },
        )


@pytest.fixture
async def database() -> AsyncGenerator[Database]:
    dsn = os.getenv("TEST_DATABASE_URL", "")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is required for Reader durability integration tests")

    database = Database(DatabaseConfig(dsn=dsn, pool_size=1, max_overflow=1))
    await database.migrate()
    async with database.transaction() as session:
        await session.execute(delete(LLMCall))
        await session.execute(delete(Summary))
        await session.execute(delete(CrawlResult))
        await session.execute(delete(Request))
    try:
        yield database
    finally:
        async with database.transaction() as session:
            await session.execute(delete(LLMCall))
            await session.execute(delete(Summary))
            await session.execute(delete(CrawlResult))
            await session.execute(delete(Request))
        await database.dispose()


@pytest.mark.asyncio
async def test_completed_url_keeps_reader_source_after_fresh_session(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from langgraph.checkpoint.memory import InMemorySaver

    requests = RequestRepositoryAdapter(database)
    crawls = CrawlResultRepositoryAdapter(database)
    summaries = SummaryRepositoryAdapter(database)
    request_id = await requests.async_create_request(
        type_="url",
        status=RequestStatus.PENDING,
        correlation_id="reader-durability",
        user_id=1402,
        input_url="https://example.com/durable-reader",
        normalized_url="https://example.com/durable-reader",
        dedupe_hash="reader-durability",
    )

    async def no_op(_state: object, *, deps: object) -> dict[str, object]:
        return {}

    async def summarize(_state: object, *, deps: object) -> dict[str, object]:
        return {"summary": {"summary_250": "Durability regression summary."}}

    async def validate(_state: object, *, deps: object) -> dict[str, object]:
        return {"validation_errors": []}

    patched_nodes = dict(graph_mod._NODES)
    for node_name in ("ground", "build_prompt", "repair", "enrich", "notify"):
        patched_nodes[node_name] = no_op
    patched_nodes["summarize"] = summarize
    patched_nodes["validate"] = validate
    monkeypatch.setattr(graph_mod, "_NODES", patched_nodes)

    extraction = ContentExtractionAdapter(
        content_extractor=_PersistingContentExtractor(crawls),  # type: ignore[arg-type]
        request_repo=requests,
    )
    deps = SummarizeDeps(
        llm_client=SimpleNamespace(),
        retrieval=SimpleNamespace(),
        extraction=extraction,
        stream_sink=SimpleNamespace(),
        summaries=summaries,
        requests=requests,
        summary_index=SimpleNamespace(index_summary=AsyncMock()),
        crawl_repo=crawls,
    )
    graph = build_summarize_graph(deps=deps, checkpointer=InMemorySaver())
    result = await run_summarize_graph(
        graph=graph,
        deps=deps,
        correlation_id="reader-durability",
        request_id=request_id,
        lang="en",
        input_url="https://example.com/durable-reader",
    )
    assert "error" not in result
    summary_id = result.get("summary_id")
    assert isinstance(summary_id, int)

    # Drop every pooled connection so the Reader read cannot observe in-memory
    # ORM state from the write phase.
    await database.dispose()
    fresh_database = Database(
        DatabaseConfig(dsn=os.environ["TEST_DATABASE_URL"], pool_size=1, max_overflow=1)
    )
    use_case = SummaryReadModelUseCase(
        SummaryRepositoryAdapter(fresh_database),
        RequestRepositoryAdapter(fresh_database),
        CrawlResultRepositoryAdapter(fresh_database),
        LLMRepositoryAdapter(fresh_database),
    )
    try:
        response = await get_summary_content(
            summary_id=summary_id,
            format="markdown",
            user={"user_id": 1402},
            use_case=use_case,
        )
    finally:
        await fresh_database.dispose()

    content = response["data"]["content"]
    assert content["content"] == _SOURCE_TEXT
    assert content["format"] == "text"
    assert content["contentType"] == "text/plain"
