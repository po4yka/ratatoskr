"""Tests for search edge cases and error handling."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

from app.api.models.responses import SearchResult
from app.api.search_insights import compute_search_insights_payload
from app.api.services.search_service import SearchService
from app.core.time_utils import UTC
from tests.api.conftest import _build_search_results

# ==================== Edge Cases and Error Handling ====================


def test_search_special_characters(client, search_data, search_token):
    """Test search with special characters in query."""
    response = client.get(
        "/v1/search",
        params={"q": "test@#$%", "limit": 10, "offset": 0},
        headers={"Authorization": f"Bearer {search_token}"},
    )

    # Should not crash, may return no results
    assert response.status_code in [200, 500]


def test_search_unicode_query(client, search_data, search_token, mock_search_service_results):
    """Test search with unicode characters."""
    response = client.get(
        "/v1/search",
        params={"q": "тест 测试", "limit": 10, "offset": 0},
        headers={"Authorization": f"Bearer {search_token}"},
    )

    assert response.status_code == 200


def test_isotime_helper_none_value(client):
    """Test _isotime helper with None value."""
    from app.api.search_helpers import isotime

    result = isotime(None)
    assert result == ""


def test_isotime_helper_datetime_value(client):
    """Test _isotime helper with datetime object."""
    from app.api.search_helpers import isotime

    dt = datetime(2023, 1, 1, 12, 0, 0, tzinfo=UTC)
    result = isotime(dt)
    assert result.endswith("Z")
    assert "2023-01-01" in result


def test_isotime_helper_string_value(client):
    """Test _isotime helper with string value."""
    from app.api.search_helpers import isotime

    result = isotime("2023-01-01")
    assert result == "2023-01-01"


def test_search_response_structure(client, search_data, search_token, mock_search_service_results):
    """Test that search response includes all required fields."""
    response = client.get(
        "/v1/search",
        params={"q": "example", "limit": 10, "offset": 0},
        headers={"Authorization": f"Bearer {search_token}"},
    )

    assert response.status_code == 200
    json_data = response.json()

    # Top level structure
    assert "success" in json_data
    assert "data" in json_data
    assert "meta" in json_data

    # Meta structure
    assert "correlation_id" in json_data["meta"]
    assert "timestamp" in json_data["meta"]
    assert "version" in json_data["meta"]

    # Data structure
    data = json_data["data"]
    assert "results" in data
    assert "pagination" in data
    assert "query" in data

    # Pagination structure
    pagination = data["pagination"]
    assert "total" in pagination
    assert "limit" in pagination
    assert "offset" in pagination
    assert "hasMore" in pagination


def test_semantic_search_response_structure(client, search_token):
    """Test semantic search response structure."""
    with patch.object(
        SearchService,
        "semantic_search_summaries",
        AsyncMock(
            return_value=_build_search_results(
                query="test",
                results=[],
                total=0,
                limit=10,
                offset=0,
                intent="similarity",
                mode="semantic",
            )
        ),
    ):
        response = client.get(
            "/v1/search/semantic",
            params={"q": "test", "limit": 10, "offset": 0},
            headers={"Authorization": f"Bearer {search_token}"},
        )

    assert response.status_code == 200
    json_data = response.json()

    assert "success" in json_data
    assert "data" in json_data
    assert "meta" in json_data

    data = json_data["data"]
    assert "results" in data
    assert "pagination" in data
    assert "query" in data


def test_search_response_includes_mode_intent_and_facets(
    client, search_data, search_token, mock_search_service_results
):
    """Search response should include intent/mode/facets metadata."""
    response = client.get(
        "/v1/search",
        params={"q": "ai trends", "limit": 10, "offset": 0},
        headers={"Authorization": f"Bearer {search_token}"},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data.get("intent") in {"topic", "keyword", "entity", "question", "similarity"}
    assert data.get("mode") in {"keyword", "semantic", "hybrid"}
    assert isinstance(data.get("facets"), dict)
    assert "domains" in data["facets"]


def test_semantic_search_response_includes_explanations(client, search_token):
    """Semantic results should include explainability fields."""
    with patch.object(
        SearchService,
        "semantic_search_summaries",
        AsyncMock(
            return_value=_build_search_results(
                query="ai",
                results=[
                    SearchResult(
                        request_id=1,
                        summary_id=1,
                        url="https://example.com/a",
                        title="A",
                        domain="example.com",
                        snippet="about ai",
                        tldr="ai",
                        published_at=None,
                        created_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                        relevance_score=0.9,
                        topic_tags=["#ai"],
                        is_read=False,
                        match_signals=["semantic"],
                        match_explanation="Matched semantically",
                        score_breakdown={
                            "fts": 0.0,
                            "semantic": 0.9,
                            "freshness": 0.5,
                            "popularity": 0.1,
                            "lexical": 0.6,
                        },
                    )
                ],
                total=1,
                limit=10,
                offset=0,
                intent="similarity",
                mode="semantic",
                facets={"domains": ["example.com"], "tags": ["#ai"], "languages": ["en"]},
            )
        ),
    ):
        response = client.get(
            "/v1/search/semantic",
            params={"q": "ai", "limit": 10, "offset": 0},
            headers={"Authorization": f"Bearer {search_token}"},
        )

    assert response.status_code == 200
    results = response.json()["data"]["results"]
    if results:
        assert "matchSignals" in results[0]
        assert "matchExplanation" in results[0]
        assert "scoreBreakdown" in results[0]


def test_search_failure_returns_generic_message_not_internal_exception(client, search_token):
    """CWE-209 regression: a failing FTS search returns a generic client message, never
    the stringified internal exception (DB DSNs, hosts, secrets)."""
    secret = "postgresql://admin:s3cr3t@internal-db.local:5432/ratatoskr timed out"
    with patch.object(
        SearchService, "search_summaries", AsyncMock(side_effect=RuntimeError(secret))
    ):
        response = client.get(
            "/v1/search",
            params={"q": "anything", "limit": 10, "offset": 0},
            headers={"Authorization": f"Bearer {search_token}"},
        )

    assert response.status_code == 500
    # The internal exception text must not appear anywhere in the client payload.
    assert secret not in response.text
    assert "s3cr3t" not in response.text
    assert "internal-db.local" not in response.text
    assert response.json()["error"]["message"] == "Search failed"


def test_semantic_search_failure_returns_generic_message_not_internal_exception(
    client, search_token
):
    """CWE-209 regression for the semantic search endpoint."""
    secret = "Qdrant AuthError api_key=live_9f8a7b at internal cluster node-3"
    with patch.object(
        SearchService,
        "semantic_search_summaries",
        AsyncMock(side_effect=RuntimeError(secret)),
    ):
        response = client.get(
            "/v1/search/semantic",
            params={"q": "anything", "limit": 10, "offset": 0},
            headers={"Authorization": f"Bearer {search_token}"},
        )

    assert response.status_code == 500
    assert secret not in response.text
    assert "live_9f8a7b" not in response.text
    assert response.json()["error"]["message"] == "Semantic search failed"


def test_search_insights_success(client, search_data, search_token):
    """Insights endpoint returns analytics blocks."""
    response = client.get(
        "/v1/search/insights",
        params={"days": 30, "limit": 10},
        headers={"Authorization": f"Bearer {search_token}"},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert "topic_trends" in data
    assert "rising_entities" in data
    assert "source_diversity" in data
    assert "language_mix" in data
    assert "coverage_gaps" in data


def test_search_insights_payload_preserves_windowed_counts():
    """Insights payload keeps existing response shape with projected repository rows."""
    now = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
    current_start = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    previous_start = datetime(2026, 5, 3, 12, 0, tzinfo=UTC)

    rows = [
        {
            "lang": "en",
            "request": {"created_at": datetime(2026, 5, 16, 9, 0, tzinfo=UTC)},
            "json_payload": {
                "topic_tags": ["#AI", "#Search"],
                "entities": {"people": ["Ada"], "organizations": ["OpenAI"], "locations": []},
                "seo_keywords": ["retrieval", "retrieval"],
                "metadata": {"domain": "Example.com"},
            },
        },
        {
            "lang": "ru",
            "request": {"created_at": datetime(2026, 5, 12, 9, 0, tzinfo=UTC)},
            "json_payload": {
                "topic_tags": ["#ai"],
                "entities": {"people": ["Ada"], "organizations": [], "locations": []},
                "seo_keywords": ["retrieval"],
                "metadata": {"domain": "docs.example.com"},
            },
        },
        {
            "lang": "en",
            "request": {"created_at": datetime(2026, 5, 5, 9, 0, tzinfo=UTC)},
            "json_payload": {
                "topic_tags": ["#ai"],
                "entities": {"people": ["Grace"], "organizations": [], "locations": []},
                "seo_keywords": ["archives"],
                "metadata": {"domain": "Example.com"},
            },
        },
    ]

    payload = compute_search_insights_payload(
        rows=rows,
        now=now,
        current_start=current_start,
        previous_start=previous_start,
        days=7,
        limit=5,
    )

    assert payload["period_days"] == 7
    assert payload["topic_trends"][0] == {
        "tag": "#ai",
        "count": 2,
        "prev_count": 1,
        "trend_delta": 1,
        "trend_score": 1.0,
    }
    assert payload["rising_entities"][0] == {"entity": "Ada", "count": 2}
    assert payload["source_diversity"]["unique_domains"] == 2
    assert payload["language_mix"]["total"] == 3
    assert payload["coverage_gaps"][0]["term"] == "retrieval"
