"""Security regression tests for aggregation extraction failures."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.agents.multi_source_extraction_agent import MultiSourceExtractionAgent
from app.application.dto.aggregation import MultiSourceExtractionInput, SourceSubmission


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
