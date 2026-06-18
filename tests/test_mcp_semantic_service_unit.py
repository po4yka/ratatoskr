from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.mcp.semantic_service import SemanticSearchService
from app.mcp.article_service import ArticleReadService
from app.mcp.context import McpServerContext


class _Context:
    user_id = 123

    async def init_vector_service(self) -> None:
        return None


class _ArticleService:
    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.payload = payload or {"results": [], "total": 0}
        self.calls: list[dict[str, Any]] = []

    async def search_articles(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.payload


def _summary(summary_id: int, *, title: str, body: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=summary_id,
        lang="en",
        is_read=False,
        is_favorited=True,
        created_at=None,
        json_payload={
            "metadata": {"title": title, "domain": "example.com"},
            "summary_250": body,
            "summary_1000": f"{body} extended",
            "tldr": f"{body} tldr",
            "topic_tags": ["#ai"],
            "seo_keywords": ["semantic"],
        },
    )


def _request(request_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=request_id,
        input_url=f"https://example.com/{request_id}",
        normalized_url=f"https://example.com/{request_id}",
    )


class _SemanticService(SemanticSearchService):
    def __init__(
        self,
        rows: list[dict[str, Any]] | None = None,
        summary_map: dict[int, tuple[Any, Any]] | None = None,
        article_payload: dict[str, Any] | None = None,
    ) -> None:
        article_service = _ArticleService(article_payload)
        super().__init__(
            cast("McpServerContext", _Context()), cast("ArticleReadService", article_service)
        )
        self.fake_article_service = article_service
        self.rows = rows or []
        self.summary_map = summary_map or {}

    async def _fetch_summaries_by_ids(self, summary_ids: list[int]) -> dict[int, tuple[Any, Any]]:
        return {summary_id: self.summary_map[summary_id] for summary_id in summary_ids}

    async def _search_local_vectors(
        self,
        query: str,
        *,
        language: str | None,
        limit: int,
        min_similarity: float,
    ) -> list[dict[str, Any]]:
        return [
            row for row in self.rows if float(row.get("similarity_score", 0.0)) >= min_similarity
        ][:limit]


def test_semantic_helpers_tokenize_tags_seed_text_and_cosine() -> None:
    payload = {
        "metadata": {"title": "Main title"},
        "summary_250": "Short",
        "summary_1000": "Long",
        "tldr": "TLDR",
        "key_ideas": ["Idea 1", "Idea 2", None],
        "topic_tags": ["#AI", "#Search"],
    }

    assert SemanticSearchService._tokenize("AI-first search, AI") == {"ai-first", "search", "ai"}
    assert SemanticSearchService._extract_query_tags("#AI topic #AI #ml") == ["#ai", "#ml"]
    assert SemanticSearchService.extract_semantic_seed_text(payload).startswith("Main title Short")
    assert SemanticSearchService._cosine_similarity([1, 0], [1, 0]) == pytest.approx(1.0)
    assert SemanticSearchService(
        cast("McpServerContext", _Context()),
        cast("ArticleReadService", _ArticleService()),
    )._lexical_overlap_score("ai search", "search only") == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_build_semantic_results_groups_chunks_dedupes_and_reranks() -> None:
    service = _SemanticService(
        summary_map={
            1: (_summary(1, title="Semantic databases", body="vector ranking"), _request(10)),
            2: (_summary(2, title="Other", body="unrelated"), _request(20)),
        }
    )
    rows = [
        {
            "summary_id": 1,
            "similarity_score": 0.7,
            "window_id": "w1",
            "chunk_id": "c1",
            "section": "body",
            "local_summary": "vector ranking preview",
            "topics": ["#ai"],
            "local_keywords": ["vector"],
        },
        {
            "summary_id": 1,
            "similarity_score": 0.9,
            "window_id": "w2",
            "chunk_id": "c2",
            "section": "body",
            "local_summary": "semantic database preview",
            "topics": ["#db"],
            "local_keywords": ["semantic"],
        },
        {
            "summary_id": 1,
            "similarity_score": 0.9,
            "window_id": "w2",
            "chunk_id": "c2",
            "section": "body",
            "local_summary": "semantic database preview",
            "topics": ["#db"],
            "local_keywords": ["semantic"],
        },
        {"summary_id": "bad", "similarity_score": 1.0},
        {"summary_id": 2, "similarity_score": 0.95, "local_summary": "other"},
    ]

    results = await service._build_semantic_results(
        query="semantic database",
        rows=rows,
        backend="unit",
        limit=5,
        include_chunks=True,
        rerank=True,
    )

    assert [row["summary_id"] for row in results] == [1, 2]
    assert results[0]["similarity_score"] == pytest.approx(0.9)
    assert results[0]["semantic_match_count"] == 2
    assert results[0]["best_match"]["chunk_id"] == "c2"
    assert results[0]["search_backend"] == "unit"
    assert results[0]["rerank_score"] > results[1]["rerank_score"]


@pytest.mark.asyncio
async def test_semantic_search_uses_local_vector_results_before_keyword_fallback() -> None:
    service = _SemanticService(
        rows=[
            {"summary_id": 3, "similarity_score": 0.8, "local_summary": "local vector match"},
        ],
        summary_map={
            3: (_summary(3, title="Local", body="local vector match"), _request(30)),
        },
        article_payload={"results": [{"summary_id": 99}], "total": 1},
    )

    payload = await service.semantic_search("local vector", min_similarity=0.5)

    assert payload["search_backend"] == "local_vector"
    assert payload["search_type"] == "semantic"
    assert payload["results"][0]["summary_id"] == 3
    assert service.fake_article_service.calls == []


@pytest.mark.asyncio
async def test_hybrid_search_fuses_semantic_and_keyword_results() -> None:
    service = _SemanticService(
        rows=[
            {"summary_id": 4, "similarity_score": 0.8, "local_summary": "semantic"},
        ],
        summary_map={
            4: (_summary(4, title="Semantic", body="semantic result"), _request(40)),
        },
        article_payload={
            "results": [
                {"summary_id": 4, "title": "same"},
                {"summary_id": 5, "title": "keyword only"},
            ],
            "total": 2,
        },
    )

    payload = await service.hybrid_search("semantic keyword", limit=2)

    assert payload["search_type"] == "hybrid"
    assert payload["semantic_backend"] == "local_vector"
    assert payload["results"][0]["summary_id"] == 4
    assert payload["results"][0]["match_sources"] == ["semantic", "keyword"]
    assert payload["results"][1]["match_sources"] == ["keyword"]


@pytest.mark.asyncio
async def test_semantic_search_keyword_fallback_when_vectors_are_empty() -> None:
    service = _SemanticService(
        article_payload={"results": [{"summary_id": 8, "title": "keyword"}], "total": 3}
    )

    payload = await service.semantic_search("keyword query", limit=1)

    assert payload["search_type"] == "keyword_fallback"
    assert payload["search_backend"] == "fts"
    assert payload["has_more"] is True
    assert payload["results"] == [{"summary_id": 8, "title": "keyword"}]
