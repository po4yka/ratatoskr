"""Security regression tests for aggregation extraction failures."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.agents.multi_source_extraction_agent import MultiSourceExtractionAgent
from app.application.dto.aggregation import MultiSourceExtractionInput, SourceSubmission
from app.domain.models.source import AggregationSessionStatus


async def test_extraction_failure_does_not_expose_upstream_error() -> None:
    secret_error = "upstream rejected Authorization: Bearer secret-token"
    extractor = SimpleNamespace(
        extract_content_pure=AsyncMock(side_effect=RuntimeError(secret_error))
    )
    repo = SimpleNamespace(
        async_create_aggregation_session=AsyncMock(return_value=1),
        async_update_aggregation_session_status=AsyncMock(),
        async_add_aggregation_session_item=AsyncMock(return_value=10),
        async_update_aggregation_session_item_result=AsyncMock(),
        async_update_aggregation_session_counts=AsyncMock(),
    )
    events: list[dict[str, object]] = []

    agent = MultiSourceExtractionAgent(
        content_extractor=extractor,
        aggregation_session_repo=repo,
    )
    result = await agent.execute(
        MultiSourceExtractionInput(
            correlation_id="cid-safe-error",
            user_id=7,
            items=[SourceSubmission.from_url("https://example.com/article")],
            progress_callback=events.append,
        )
    )

    assert result.success is False
    failed_update = repo.async_update_aggregation_session_item_result.await_args
    failure = failed_update.kwargs["failure"]
    assert failure.message == "Source extraction failed. Error ID: cid-safe-error"
    assert "secret-token" not in failure.model_dump_json()

    failed_event = next(event for event in events if event["event"] == "item_failed")
    assert failed_event["error"] == "Source extraction failed. Error ID: cid-safe-error"
    assert "secret-token" not in str(events)


def test_failed_source_with_duplicate_resolves_session_as_failed() -> None:
    status = MultiSourceExtractionAgent._resolve_session_status(
        successful_count=0,
        failed_count=1,
        duplicate_count=1,
    )

    assert status is AggregationSessionStatus.FAILED


def test_partial_extraction_is_failed_when_partial_success_is_disabled() -> None:
    status = MultiSourceExtractionAgent._resolve_session_status(
        successful_count=1,
        failed_count=1,
        duplicate_count=0,
        allow_partial_success=False,
    )

    assert status is AggregationSessionStatus.FAILED


async def test_disallowed_partial_result_is_persisted_and_returned_as_failed() -> None:
    extractor = SimpleNamespace(
        extract_content_pure=AsyncMock(
            side_effect=[
                ("extracted body", "test", {"title": "First"}),
                RuntimeError("upstream failed"),
            ]
        )
    )
    repo = SimpleNamespace(
        async_create_aggregation_session=AsyncMock(return_value=1),
        async_update_aggregation_session_status=AsyncMock(),
        async_add_aggregation_session_item=AsyncMock(side_effect=[10, 11]),
        async_update_aggregation_session_item_result=AsyncMock(),
        async_update_aggregation_session_counts=AsyncMock(),
    )
    events: list[dict[str, object]] = []
    agent = MultiSourceExtractionAgent(
        content_extractor=extractor,
        aggregation_session_repo=repo,
    )

    result = await agent.execute(
        MultiSourceExtractionInput(
            correlation_id="cid-no-partial",
            user_id=7,
            items=[
                SourceSubmission.from_url("https://example.com/one"),
                SourceSubmission.from_url("https://example.com/two"),
            ],
            allow_partial_success=False,
            progress_callback=events.append,
        )
    )

    assert result.success is False
    assert result.metadata["status"] == AggregationSessionStatus.FAILED.value
    terminal_update = repo.async_update_aggregation_session_status.await_args_list[-1]
    assert terminal_update.kwargs["status"] is AggregationSessionStatus.FAILED
    assert terminal_update.kwargs["failure"].code == "partial_success_not_allowed"
    completed_event = next(event for event in events if event["event"] == "session_completed")
    assert completed_event["status"] == AggregationSessionStatus.FAILED.value
