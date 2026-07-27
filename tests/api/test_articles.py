"""Article endpoint tests using the direct-call pattern (no HTTP client)."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from app.api.dependencies.database import get_summary_read_model_use_case
from app.api.exceptions import ExternalAPIError, ResourceNotFoundError
from app.api.routers.content.summaries import (
    backfill_summary_content,
    get_summary,
    get_summary_by_url,
    get_summary_content,
)
from app.application.services.source_content_backfill_service import (
    SourceContentBackfillFailedError,
)
from app.core.summary_schema import SummaryModel
from app.db.models import Request, Summary


def _summary_context(
    *,
    metadata: dict[str, Any],
    crawl_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = datetime(2026, 7, 26, tzinfo=UTC)
    return {
        "summary": {
            "id": 1402,
            "json_payload": {
                "summary_250": "Short summary",
                "summary_1000": "Long summary",
                "tldr": "TLDR",
                "key_ideas": [],
                "topic_tags": [],
                "entities": {},
                "estimated_reading_time_min": 12,
                "metadata": metadata,
            },
        },
        "request": {
            "id": 42,
            "type": "url",
            "status": "completed",
            "input_url": "https://example.com/article",
            "normalized_url": "https://example.com/article",
            "created_at": now,
            "updated_at": now,
        },
        "request_id": 42,
        "crawl_result": crawl_result,
        "transcription_artifact": None,
        "llm_calls": [],
        "aggregation_source_bundle": None,
    }


@pytest_asyncio.fixture
async def article_user(db, user_factory):
    return await user_factory(telegram_user_id=123456789, username="article_test_user")


@pytest_asyncio.fixture
async def article_data(db, article_user):
    full_payload: dict[str, Any] = {
        "summary_250": "Short summary",
        "summary_1000": "Long summary",
        "tldr": "Too long",
        "key_ideas": ["Idea 1", "Idea 2"],
        "topic_tags": ["tag1", "tag2"],
        "entities": {"people": ["Person"], "organizations": ["Org"], "locations": ["Loc"]},
        "estimated_reading_time_min": 5,
        "key_stats": [{"label": "Stat", "value": 10, "unit": "%", "source_excerpt": "source"}],
        "answered_questions": ["Q1?"],
        "readability": {"method": "FK", "score": 50.0, "level": "Easy"},
        "seo_keywords": ["keyword"],
        "metadata": {
            "title": "Example Article",
            "domain": "example.com",
            "author": "Author",
            "published_at": "2023-01-01",
        },
        "confidence": 0.9,
        "hallucination_risk": "low",
        "summary_quality": {
            "validation_warnings": ["confidence_invalid"],
            "repair_attempted": True,
            "repair_succeeded": True,
            "structured_output_mode": "json_object",
            "model_used": "safe-model",
            "source_coverage": "partial",
            "extraction_confidence": 0.7,
            "prompt_injection_suspected": True,
            "raw_prompt": "must never appear",
            "raw_llm_output": "must never appear",
        },
    }

    async with db.transaction() as session:
        req = Request(
            user_id=article_user.telegram_user_id,
            type="url",
            status="completed",
            input_url="https://example.com/article",
            normalized_url="https://example.com/article",
        )
        session.add(req)
        await session.flush()

        summary = Summary(
            request_id=req.id,
            lang="en",
            json_payload=full_payload,
        )
        session.add(summary)
        await session.flush()

    return {"user": article_user, "request": req, "summary": summary}


@pytest.mark.asyncio
async def test_get_article_by_id(db, article_data):
    user = article_data["user"]
    summary = article_data["summary"]

    user_ctx = {
        "user_id": user.telegram_user_id,
        "username": user.username,
        "client_id": "test",
    }
    use_case = get_summary_read_model_use_case(session_manager=db)

    result = await get_summary(summary_id=summary.id, user=user_ctx, use_case=use_case)

    data = result["data"]
    assert data["summary"]["tldr"] == "Too long"
    assert data["request"]["url"] == "https://example.com/article"
    quality = data["processing"]["quality"]
    assert quality["sourceCoverage"] == "partial"
    assert quality["repairAttempted"] is True
    assert quality["repairSucceeded"] is True
    assert quality["promptInjectionSuspected"] is True
    assert "rawPrompt" not in quality
    assert "raw_prompt" not in quality
    assert "rawLlmOutput" not in quality
    assert "raw_llm_output" not in quality
    assert set(data["processingResults"]) == set(SummaryModel.model_fields)
    assert data["processingResults"]["key_ideas"] == ["Idea 1", "Idea 2"]
    assert "raw_prompt" not in data["processingResults"]["summary_quality"]


@pytest.mark.asyncio
async def test_get_article_title_falls_back_to_summary_metadata():
    use_case = AsyncMock()
    use_case.get_summary_context_for_user.return_value = _summary_context(
        metadata={"title": "Coroutine history"},
    )

    result = await get_summary(
        summary_id=1402,
        user={"user_id": 1},
        use_case=use_case,
    )

    assert result["data"]["source"]["title"] == "Coroutine history"


@pytest.mark.asyncio
async def test_get_article_source_falls_back_without_a_crawl_result():
    use_case = AsyncMock()
    use_case.get_summary_context_for_user.return_value = _summary_context(
        metadata={
            "title": "Coroutine history",
            "author": "Dmitry Popov",
            "published_at": "2026-07-25",
        },
    )

    result = await get_summary(
        summary_id=1402,
        user={"user_id": 1},
        use_case=use_case,
    )

    assert result["data"]["source"] == {
        "url": "https://example.com/article",
        "title": "Coroutine history",
        "domain": "example.com",
        "author": "Dmitry Popov",
        "publishedAt": "2026-07-25",
        "wordCount": None,
        "contentType": None,
        "transcript": None,
    }


@pytest.mark.asyncio
async def test_get_article_content_uses_submitted_text_without_a_crawl_result():
    use_case = AsyncMock()
    context = _summary_context(metadata={"title": "Submitted note"})
    context["request"]["content_text"] = "The complete submitted source text."
    use_case.get_summary_context_for_user.return_value = context

    result = await get_summary_content(
        summary_id=1402,
        format="markdown",
        user={"user_id": 1},
        use_case=use_case,
    )

    content = result["data"]["content"]
    assert content["content"] == "The complete submitted source text."
    assert content["format"] == "text"
    assert content["contentType"] == "text/plain"


@pytest.mark.asyncio
async def test_get_article_content_prefers_normalized_durable_artifact():
    use_case = AsyncMock()
    context = _summary_context(
        metadata={"title": "Normalized article"},
        crawl_result={
            "content_text": "Normalized complete article.",
            "content_markdown": "# Provider raw article",
        },
    )
    use_case.get_summary_context_for_user.return_value = context

    result = await get_summary_content(
        summary_id=1402,
        format="markdown",
        user={"user_id": 1},
        use_case=use_case,
    )

    content = result["data"]["content"]
    assert content["content"] == "Normalized complete article."
    assert content["format"] == "text"


@pytest.mark.asyncio
async def test_get_article_content_uses_transcript_without_a_crawl_result():
    use_case = AsyncMock()
    context = _summary_context(metadata={"title": "Voice note"})
    context["transcription_artifact"] = {"plain_text": "The complete voice transcript."}
    use_case.get_summary_context_for_user.return_value = context

    result = await get_summary_content(
        summary_id=1402,
        format="markdown",
        user={"user_id": 1},
        use_case=use_case,
    )

    content = result["data"]["content"]
    assert content["content"] == "The complete voice transcript."
    assert content["format"] == "text"
    assert content["contentType"] == "text/plain"


@pytest.mark.asyncio
async def test_backfill_article_content_returns_restored_body():
    use_case = AsyncMock()
    context = _summary_context(metadata={"title": "Restored article"})
    context["request"]["content_text"] = "The restored source body."
    use_case.get_summary_context_for_user.return_value = context
    service = AsyncMock()

    result = await backfill_summary_content(
        summary_id=1402,
        request=SimpleNamespace(state=SimpleNamespace(correlation_id="operation-cid")),
        user={"user_id": 1},
        service=service,
        use_case=use_case,
    )

    service.backfill.assert_awaited_once_with(
        user_id=1,
        summary_id=1402,
        operation_correlation_id="operation-cid",
    )
    assert result["data"]["content"]["content"] == "The restored source body."


@pytest.mark.asyncio
async def test_backfill_article_content_maps_extraction_failure():
    service = AsyncMock()
    service.backfill.side_effect = SourceContentBackfillFailedError(1402)

    with pytest.raises(ExternalAPIError) as exc_info:
        await backfill_summary_content(
            summary_id=1402,
            request=SimpleNamespace(state=SimpleNamespace(correlation_id="operation-cid")),
            user={"user_id": 1},
            service=service,
            use_case=AsyncMock(),
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_get_article_by_url(db, article_data):
    user = article_data["user"]

    user_ctx = {
        "user_id": user.telegram_user_id,
        "username": user.username,
        "client_id": "test",
    }
    use_case = get_summary_read_model_use_case(session_manager=db)

    url = "https://example.com/article"
    result = await get_summary_by_url(url=url, user=user_ctx, use_case=use_case)

    data = result["data"]
    assert data["summary"]["tldr"] == "Too long"
    assert data["request"]["url"] == url


@pytest.mark.asyncio
async def test_get_article_by_url_not_found(db, article_data):
    user = article_data["user"]

    user_ctx = {
        "user_id": user.telegram_user_id,
        "username": user.username,
        "client_id": "test",
    }
    use_case = get_summary_read_model_use_case(session_manager=db)

    with pytest.raises(ResourceNotFoundError):
        await get_summary_by_url(url="https://nonexistent.com", user=user_ctx, use_case=use_case)
