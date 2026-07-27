from datetime import datetime
from types import SimpleNamespace

from app.api.search_helpers import SearchFilters
from app.api.search_ranking import (
    build_ranked_search_rows,
    build_semantic_filtered_rows,
    rows_to_search_results,
)
from app.core.time_utils import UTC


def _request(request_id: int) -> dict[str, object]:
    return {
        "id": request_id,
        "input_url": f"https://example.com/{request_id}",
        "normalized_url": f"https://example.com/{request_id}",
        "created_at": datetime(2026, 7, 27, tzinfo=UTC),
    }


def _summary(summary_id: int, title: object) -> dict[str, object]:
    return {
        "id": summary_id,
        "lang": "en",
        "is_read": False,
        "is_favorited": False,
        "json_payload": {
            "metadata": {"title": title, "domain": "example.com"},
            "summary_250": "Searchable summary.",
            "tldr": "Searchable.",
            "topic_tags": ["#search"],
        },
    }


def test_keyword_ranking_normalizes_invalid_titles_without_losing_results() -> None:
    rows = build_ranked_search_rows(
        q="search",
        resolved_mode="keyword",
        candidate_request_ids=[1, 2],
        requests_map={1: _request(1), 2: _request(2)},
        summaries_map={1: _summary(101, None), 2: _summary(102, "Metadata title")},
        fts_by_request_id={
            1: {
                "score": 0.9,
                "row": {
                    "title": {"invalid": True},
                    "snippet": "search result",
                    "source": "example.com",
                },
            },
            2: {
                "score": 0.8,
                "row": {
                    "title": "FTS title",
                    "snippet": "search result",
                    "source": "example.com",
                },
            },
        },
        semantic_by_request_id={},
        filters=SearchFilters(),
    )

    results = rows_to_search_results(rows)

    assert [result.title for result in results] == ["Untitled", "FTS title"]


def test_semantic_ranking_normalizes_invalid_titles_without_losing_results() -> None:
    search_results = SimpleNamespace(
        results=[
            SimpleNamespace(
                request_id=1,
                summary_id=101,
                similarity_score=0.9,
                url=None,
                title=None,
                snippet="semantic search result",
                tags=[],
            ),
            SimpleNamespace(
                request_id=2,
                summary_id=102,
                similarity_score=0.8,
                url=None,
                title="Vector title",
                snippet="semantic search result",
                tags=[],
            ),
        ]
    )

    rows = build_semantic_filtered_rows(
        q="search",
        min_similarity=0.3,
        filters=SearchFilters(),
        search_results=search_results,
        requests_map={1: _request(1), 2: _request(2)},
        summaries_map={1: _summary(101, ["invalid"]), 2: _summary(102, "Metadata title")},
    )
    results = rows_to_search_results(rows)

    assert [result.title for result in results] == ["Untitled", "Vector title"]
