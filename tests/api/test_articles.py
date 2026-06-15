"""Article endpoint tests using the direct-call pattern (no HTTP client)."""

from typing import Any

import pytest
import pytest_asyncio

from app.api.dependencies.database import get_summary_read_model_use_case
from app.api.exceptions import ResourceNotFoundError
from app.api.routers.content.summaries import get_summary, get_summary_by_url
from app.db.models import Request, Summary


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
