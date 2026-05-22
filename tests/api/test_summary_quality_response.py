from datetime import datetime
from typing import Any

import pytest

from app.api.routers.content.summaries import get_summaries, get_summary
from app.api.routers.content.summaries import _build_summary_compact
from app.api.routers.content.summaries import _safe_summary_quality
from app.core.time_utils import UTC


class FakeSummaryReadModelUseCase:
    def __init__(self) -> None:
        self.summary = {
            "id": 99,
            "request_id": 42,
            "lang": "en",
            "json_payload": {
                "summary_250": "A short backend contract summary.",
                "summary_1000": "A longer backend contract summary.",
                "tldr": "Backend summary detail keeps quality nested under processing.",
                "key_ideas": ["quality metadata is public-safe"],
                "topic_tags": ["contracts"],
                "entities": {"people": ["Alice"], "organizations": ["Acme"], "locations": []},
                "estimated_reading_time_min": 4,
                "key_stats": [{"label": "Contracts", "value": 3, "unit": "checks", "source_excerpt": "source"}],
                "metadata": {"title": "Backend Contract", "domain": "example.com"},
                "confidence": 0.87,
                "hallucination_risk": "med",
                "summary_quality": {
                    "validation_warnings": ["coverage_partial"],
                    "repair_attempted": True,
                    "repair_succeeded": False,
                    "structured_output_mode": "json_schema",
                    "model_used": "contract-model",
                    "source_coverage": "partial",
                    "extraction_quality": "medium",
                    "extraction_confidence": 0.73,
                    "prompt_injection_suspected": False,
                    "raw_prompt": "must not leak",
                },
            },
            "created_at": datetime(2026, 5, 21, tzinfo=UTC),
            "is_read": False,
            "is_favorited": True,
        }
        self.request = {
            "id": 42,
            "type": "url",
            "input_url": "https://example.com/backend-contract",
            "normalized_url": "https://example.com/backend-contract",
            "status": "completed",
            "created_at": datetime(2026, 5, 21, tzinfo=UTC),
            "updated_at": datetime(2026, 5, 21, tzinfo=UTC),
        }
        self.crawl_result = {
            "source_url": "https://example.com/backend-contract",
            "metadata_json": {"title": "Backend Contract", "domain": "example.com"},
            "latency_ms": 120,
        }
        self.llm_calls = [
            {
                "model": "contract-model",
                "tokens_prompt": 100,
                "tokens_completion": 50,
                "latency_ms": 321,
            }
        ]
        self.aggregation_source_bundle: dict[str, Any] | None = None

    async def get_summary_context_for_user(
        self,
        *,
        user_id: int,
        summary_id: int,
    ) -> dict[str, Any] | None:
        return {
            "summary": {**self.summary, "id": summary_id},
            "request": self.request,
            "request_id": self.request["id"],
            "crawl_result": self.crawl_result,
            "llm_calls": self.llm_calls,
            "aggregation_source_bundle": self.aggregation_source_bundle,
        }

    async def get_user_summaries(self, **_kwargs: Any) -> tuple[list[dict[str, Any]], int, int]:
        return [
            {
                **self.summary,
                "request": self.request,
            }
        ], 1, 1


def test_summary_detail_quality_exposes_safe_fields_only() -> None:
    quality = _safe_summary_quality(
        {
            "validation_warnings": ["confidence_invalid"],
            "repair_attempted": True,
            "repair_succeeded": True,
            "structured_output_mode": "json_object",
            "model_used": "safe-model",
            "source_coverage": "partial",
            "extraction_quality": "medium",
            "extraction_confidence": 0.7,
            "prompt_injection_suspected": True,
            "raw_prompt": "must never appear",
            "raw_llm_output": "must never appear",
        }
    ).model_dump(by_alias=True, exclude_none=True)

    assert quality["sourceCoverage"] == "partial"
    assert quality["repairAttempted"] is True
    assert quality["repairSucceeded"] is True
    assert quality["promptInjectionSuspected"] is True
    assert quality["validationWarnings"] == ["confidence_invalid"]
    assert "rawPrompt" not in quality
    assert "raw_prompt" not in quality
    assert "rawLlmOutput" not in quality
    assert "raw_llm_output" not in quality


@pytest.mark.asyncio
async def test_summary_detail_contract_includes_processing_quality() -> None:
    response = await get_summary(
        summary_id=99,
        user={"user_id": 7},
        use_case=FakeSummaryReadModelUseCase(),  # type: ignore[arg-type]
    )

    data = response["data"]
    assert data["summary"]["summary250"] == "A short backend contract summary."
    assert data["request"]["id"] == "42"
    assert data["processing"]["hallucinationRisk"] == "medium"
    assert data["processing"]["quality"] == {
        "validationWarnings": ["coverage_partial"],
        "repairAttempted": True,
        "repairSucceeded": False,
        "structuredOutputMode": "json_schema",
        "modelUsed": "contract-model",
        "sourceCoverage": "partial",
        "extractionQuality": "medium",
        "extractionConfidence": 0.73,
        "promptInjectionSuspected": False,
    }
    assert "rawPrompt" not in data["processing"]["quality"]


@pytest.mark.asyncio
async def test_summary_detail_exposes_mixed_source_bundle_provenance() -> None:
    use_case = FakeSummaryReadModelUseCase()
    use_case.aggregation_source_bundle = {
        "session": {
            "id": 501,
            "correlation_id": "agg-summary-501",
            "status": "partial",
        },
        "items": [
            {
                "id": 701,
                "aggregation_session_id": 501,
                "request_id": 42,
                "position": 0,
                "source_kind": "web_article",
                "source_item_id": "src_ok",
                "original_value": "https://example.com/backend-contract",
                "normalized_value": "https://example.com/backend-contract",
                "status": "extracted",
                "summary_id": 99,
                "crawl_result_id": 88,
                "normalized_document_json": {
                    "metadata": {"title": "Backend Contract", "domain": "example.com"}
                },
            },
            {
                "id": 702,
                "aggregation_session_id": 501,
                "request_id": None,
                "position": 1,
                "source_kind": "web_article",
                "source_item_id": "src_failed",
                "original_value": "https://bad.example/article",
                "normalized_value": "https://bad.example/article",
                "status": "failed",
                "failure_code": "FETCH_TIMEOUT",
                "failure_message": "Timed out fetching source",
            },
        ],
    }

    response = await get_summary(
        summary_id=99,
        user={"user_id": 7},
        use_case=use_case,  # type: ignore[arg-type]
    )

    source_bundle = response["data"]["sourceBundle"]
    assert source_bundle["bundleId"] == 501
    assert source_bundle["correlationId"] == "agg-summary-501"
    assert source_bundle["status"] == "partial"
    assert [
        (item["sourceItemId"], item["extractionStatus"]) for item in source_bundle["items"]
    ] == [("src_ok", "extracted"), ("src_failed", "failed")]
    assert source_bundle["items"][0]["summaryId"] == 99
    assert source_bundle["items"][0]["crawlResultId"] == 88
    assert source_bundle["items"][1]["summaryId"] is None
    assert source_bundle["items"][1]["errorCode"] == "FETCH_TIMEOUT"


@pytest.mark.asyncio
async def test_summary_list_contract_uses_mapper_shape() -> None:
    response = await get_summaries(
        limit=20,
        offset=0,
        user={"user_id": 7},
        use_case=FakeSummaryReadModelUseCase(),  # type: ignore[arg-type]
    )

    data = response["data"]
    assert data["summaries"] == [
        {
            "id": 99,
            "requestId": 42,
            "title": "Backend Contract",
            "domain": "example.com",
            "url": "https://example.com/backend-contract",
            "tldr": "Backend summary detail keeps quality nested under processing.",
            "summary250": "A short backend contract summary.",
            "readingTimeMin": 4,
            "topicTags": ["contracts"],
            "isRead": False,
            "isFavorited": True,
            "lang": "en",
            "createdAt": "2026-05-21T00:00:00+00:00Z",
            "confidence": 0.87,
            "hallucinationRisk": "medium",
            "imageUrl": None,
            "sourceCoverage": "partial",
            "repairAttempted": True,
            "repairSucceeded": False,
            "promptInjectionSuspected": False,
            "validationWarningCount": 1,
        }
    ]
    assert data["pagination"] == {"total": 1, "limit": 20, "offset": 0, "hasMore": False}
    assert data["stats"] == {"totalSummaries": 1, "unreadCount": 1}


def test_summary_list_quality_falls_back_to_quality_payload() -> None:
    compact = _build_summary_compact(
        {
            "id": 100,
            "lang": "en",
            "json_payload": {
                "summary_250": "Short.",
                "tldr": "TLDR.",
                "estimated_reading_time_min": 1,
                "topic_tags": [],
                "metadata": {"title": "Fallback Quality", "domain": "example.com"},
                "quality": {
                    "validation_warnings": ["partial"],
                    "repair_attempted": True,
                    "repair_succeeded": True,
                    "source_coverage": "partial",
                    "prompt_injection_suspected": True,
                },
            },
            "request": {
                "id": 50,
                "input_url": "https://example.com/fallback",
                "normalized_url": "https://example.com/fallback",
            },
            "created_at": datetime(2026, 5, 21, tzinfo=UTC),
        }
    ).model_dump(by_alias=True)

    assert compact["sourceCoverage"] == "partial"
    assert compact["repairAttempted"] is True
    assert compact["repairSucceeded"] is True
    assert compact["promptInjectionSuspected"] is True
    assert compact["validationWarningCount"] == 1
