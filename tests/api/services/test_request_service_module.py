from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest

from app.api.background.progress_events import ProgressEventRecord
from app.application.services.request_service import RequestService
from app.core.time_utils import UTC
from app.db.models import CrawlResult, LLMCall, Request, Summary
from app.domain.exceptions.domain_exceptions import (
    DuplicateResourceError,
    ResourceNotFoundError,
    ValidationError,
)
from tests.api.request_service_helpers import build_request_service

if TYPE_CHECKING:
    from app.application.ports import RequestRepositoryPort


class _OptimizedRequestRepository:
    def __init__(self, context: dict[str, object] | None) -> None:
        self.get_request_context_mock = AsyncMock(return_value=context)
        self.async_get_request_by_id = AsyncMock()

    async def async_get_request_context(self, request_id: int) -> dict[str, object] | None:
        return await self.get_request_context_mock(request_id)


class _ProgressEventRepository:
    def __init__(self, event: ProgressEventRecord | None) -> None:
        self.get_latest = AsyncMock(return_value=event)


def _create_request(
    *,
    user_id: int,
    status: str,
    correlation_id: str,
    created_at: datetime | None = None,
) -> Request:
    url = f"https://{correlation_id}.example.com"
    return Request.create(  # type: ignore[attr-defined]
        user_id=user_id,
        input_url=url,
        normalized_url=url,
        dedupe_hash=f"hash-{correlation_id}",
        correlation_id=correlation_id,
        status=status,
        type="url",
        created_at=created_at or datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_create_url_request_and_duplicate_detection(db, user_factory) -> None:
    user = user_factory(username="request-user", telegram_user_id=5001)
    service = build_request_service(db)

    created = await service.create_url_request(
        user.telegram_user_id,
        "example.com/articles/123",
        lang_preference="en",
    )
    Summary.create(  # type: ignore[attr-defined]
        request=created.id,
        lang="en",
        json_payload={"tldr": "TLDR", "summary_250": "Summary text", "key_ideas": ["idea"]},
    )

    duplicate = await service.check_duplicate_url(user.telegram_user_id, "example.com/articles/123")

    assert created.type == "url"
    assert created.normalized_url == "http://example.com/articles/123"
    assert duplicate is not None
    assert duplicate.existing_request_id == created.id
    assert duplicate.existing_summary_id is not None

    with pytest.raises(DuplicateResourceError):
        await service.create_url_request(user.telegram_user_id, "example.com/articles/123")


@pytest.mark.asyncio
async def test_create_forward_request_and_get_request_by_id_with_related_records(
    db,
    user_factory,
) -> None:
    user = user_factory(username="forward-user", telegram_user_id=5002)
    service = build_request_service(db)
    request = await service.create_forward_request(
        user.telegram_user_id,
        "Forwarded content",
        from_chat_id=111,
        from_message_id=222,
        lang_preference="ru",
    )

    CrawlResult.create(request=request.id, status="ok", source_url="https://example.com/post")  # type: ignore[attr-defined]
    LLMCall.create(request=request.id, status="ok", response_text="LLM output")  # type: ignore[attr-defined]
    Summary.create(  # type: ignore[attr-defined]
        request=request.id,
        lang="ru",
        json_payload={"tldr": "TLDR", "summary_250": "Summary", "key_ideas": ["idea"]},
    )

    details = await service.get_request_by_id(user.telegram_user_id, request.id)

    assert details.request.id == request.id
    assert details.crawl_result is not None
    assert details.crawl_result.source_url == "https://example.com/post"
    assert len(details.llm_calls) == 1
    assert details.summary is not None
    assert details.summary.lang == "ru"

    with pytest.raises(ResourceNotFoundError):
        await service.get_request_by_id(9999, request.id)


@pytest.mark.asyncio
async def test_retry_failed_request_requires_error_status_and_copies_fields(
    db, user_factory
) -> None:
    user = user_factory(username="retry-user", telegram_user_id=5003)
    service = build_request_service(db)
    failed = Request.create(  # type: ignore[attr-defined]
        user_id=user.telegram_user_id,
        input_url="https://retry.example.com",
        normalized_url="https://retry.example.com",
        dedupe_hash="hash-1",
        content_text="payload",
        fwd_from_chat_id=333,
        fwd_from_msg_id=444,
        lang_detected="en",
        correlation_id="cid-1",
        status="error",
        type="url",
    )

    retried = await service.retry_failed_request(user.telegram_user_id, failed.id)

    assert retried.status == "pending"
    assert retried.input_url == failed.input_url
    assert retried.correlation_id == "cid-1-retry-1"

    pending = Request.create(  # type: ignore[attr-defined]
        user_id=user.telegram_user_id,
        input_url="https://pending.example.com",
        normalized_url="https://pending.example.com",
        status="pending",
        type="url",
    )

    with pytest.raises(ValidationError, match="Only failed requests"):
        await service.retry_failed_request(user.telegram_user_id, pending.id)


@pytest.mark.asyncio
async def test_get_request_status_covers_processing_queue_complete_cancelled_and_unknown(
    db,
    user_factory,
) -> None:
    user = user_factory(username="status-user", telegram_user_id=5004)
    service = build_request_service(db)
    now = datetime.now(UTC)

    crawling = _create_request(
        user_id=user.telegram_user_id,
        status="processing",
        correlation_id="cid-crawling",
        created_at=now,
    )
    processing = _create_request(
        user_id=user.telegram_user_id,
        status="processing",
        correlation_id="cid-processing",
        created_at=now + timedelta(seconds=1),
    )
    CrawlResult.create(  # type: ignore[attr-defined]
        request=processing.id, status="ok", source_url="https://example.com/processing"
    )
    almost_done = _create_request(
        user_id=user.telegram_user_id,
        status="processing",
        correlation_id="cid-almost-done",
        created_at=now + timedelta(seconds=2),
    )
    CrawlResult.create(  # type: ignore[attr-defined]
        request=almost_done.id, status="ok", source_url="https://example.com/almost-done"
    )
    LLMCall.create(request=almost_done.id, status="ok", response_text="Generated summary")  # type: ignore[attr-defined]
    Summary.create(  # type: ignore[attr-defined]
        request=almost_done.id,
        lang="en",
        json_payload={"tldr": "TLDR", "summary_250": "Summary", "key_ideas": ["idea"]},
    )

    _create_request(
        user_id=user.telegram_user_id,
        status="pending",
        correlation_id="cid-older-pending",
        created_at=now + timedelta(seconds=3),
    )
    queued = _create_request(
        user_id=user.telegram_user_id,
        status="pending",
        correlation_id="cid-queued",
        created_at=now + timedelta(seconds=4),
    )
    complete = _create_request(
        user_id=user.telegram_user_id,
        status="ok",
        correlation_id="cid-complete",
        created_at=now + timedelta(seconds=5),
    )
    cancelled = _create_request(
        user_id=user.telegram_user_id,
        status="cancelled",
        correlation_id="cid-cancelled",
        created_at=now + timedelta(seconds=6),
    )
    unknown = _create_request(
        user_id=user.telegram_user_id,
        status="mystery",
        correlation_id="cid-unknown",
        created_at=now + timedelta(seconds=7),
    )

    crawling_status = await service.get_request_status(user.telegram_user_id, crawling.id)
    processing_status = await service.get_request_status(user.telegram_user_id, processing.id)
    almost_done_status = await service.get_request_status(user.telegram_user_id, almost_done.id)
    queued_status = await service.get_request_status(user.telegram_user_id, queued.id)
    complete_status = await service.get_request_status(user.telegram_user_id, complete.id)
    cancelled_status = await service.get_request_status(user.telegram_user_id, cancelled.id)
    unknown_status = await service.get_request_status(user.telegram_user_id, unknown.id)

    assert crawling_status.status == "running"
    assert crawling_status.stage == "extracting"
    assert crawling_status.progress == {"current_step": 1, "total_steps": 3, "percentage": 33}
    assert processing_status.status == "running"
    assert processing_status.stage == "summarizing"
    assert processing_status.progress == {"current_step": 2, "total_steps": 3, "percentage": 66}
    assert almost_done_status.progress == {"current_step": 3, "total_steps": 3, "percentage": 90}
    assert queued_status.status == "pending"
    assert queued_status.stage == "queued"
    assert queued_status.queue_position == 2
    assert complete_status.status == "succeeded"
    assert complete_status.stage == "done"
    assert cancelled_status.status == "cancelled"
    assert cancelled_status.stage == "done"
    assert cancelled_status.error_details is not None
    assert cancelled_status.error_details.error_message == "Request was cancelled"
    assert cancelled_status.error_details.error_reason_code == "REQUEST_CANCELLED"
    assert cancelled_status.can_retry is True
    assert unknown_status.status == "pending"
    assert unknown_status.stage == "queued"


@pytest.mark.asyncio
async def test_get_request_status_falls_back_for_failed_requests_and_enforces_access(
    db,
    user_factory,
) -> None:
    user = user_factory(username="status-error-user", telegram_user_id=5005)
    service = build_request_service(db)
    failed = _create_request(
        user_id=user.telegram_user_id,
        status="error",
        correlation_id="cid-error",
    )

    status = await service.get_request_status(user.telegram_user_id, failed.id)

    assert status.status == "failed"
    assert status.stage == "done"
    assert status.error_details is not None
    assert status.error_details.error_message == "Request failed"
    assert status.error_details.retryable is False
    assert status.can_retry is True

    with pytest.raises(ResourceNotFoundError):
        await service.get_request_status(999999, failed.id)


@pytest.mark.asyncio
async def test_get_request_by_id_prefers_joined_repository_path() -> None:
    request_repo = _OptimizedRequestRepository(
        {
            "request": {"id": 99, "user_id": 5002, "status": "ok", "type": "url"},
            "crawl_result": {"request": 99, "source_url": "https://example.com/joined"},
            "summary": {"id": 199, "request": 99, "lang": "en", "json_payload": {}},
        }
    )
    llm_repo = AsyncMock()
    llm_repo.async_get_llm_calls_by_request.return_value = [{"id": 3, "request": 99}]

    service = RequestService(
        db=None,
        request_repository=cast("RequestRepositoryPort", request_repo),
        summary_repository=AsyncMock(),
        crawl_result_repository=AsyncMock(),
        llm_repository=llm_repo,
    )

    details = await service.get_request_by_id(user_id=5002, request_id=99)

    assert details.request.id == 99
    assert details.crawl_result is not None
    assert details.crawl_result.source_url == "https://example.com/joined"
    assert details.summary is not None
    assert details.summary.id == 199
    assert details.llm_calls[0].id == 3
    request_repo.get_request_context_mock.assert_awaited_once_with(99)
    request_repo.async_get_request_by_id.assert_not_called()
    llm_repo.async_get_llm_calls_by_request.assert_awaited_once_with(99)


@pytest.mark.asyncio
async def test_get_request_status_uses_durable_progress_projection() -> None:
    request_repo = _OptimizedRequestRepository(
        {
            "request": {
                "id": 101,
                "user_id": 5002,
                "status": "pending",
                "type": "url",
                "correlation_id": "cid-progress",
            },
            "crawl_result": None,
            "summary": None,
        }
    )
    progress_repo = _ProgressEventRepository(
        ProgressEventRecord(
            event_id="event-101-2",
            request_id=101,
            sequence=2,
            kind="stage",
            stage="summarizing",
            status="running",
            message="Summarizing content...",
            progress=0.5,
            payload={"step": "summary"},
            created_at=datetime.now(UTC),
            correlation_id="cid-progress",
        )
    )

    service = RequestService(
        db=None,
        request_repository=cast("RequestRepositoryPort", request_repo),
        summary_repository=AsyncMock(),
        crawl_result_repository=AsyncMock(),
        llm_repository=AsyncMock(),
        progress_event_repository=progress_repo,
    )

    status = await service.get_request_status(user_id=5002, request_id=101)

    assert status.status == "running"
    assert status.stage == "summarizing"
    assert status.progress == {"percentage": 50, "value": 0.5}
    progress_repo.get_latest.assert_awaited_once_with(101)


@pytest.mark.asyncio
async def test_retry_creates_new_row_not_mutates_original(db, user_factory) -> None:
    """retry_failed_request must insert a fresh row, not overwrite the failed one."""
    user = user_factory(username="retry-new-row", telegram_user_id=7001)
    service = build_request_service(db)
    failed = Request.create(  # type: ignore[attr-defined]
        user_id=user.telegram_user_id,
        input_url="https://new-row.example.com",
        normalized_url="https://new-row.example.com",
        dedupe_hash="hash-new-row",
        status="error",
        correlation_id="cid-new-row",
        type="url",
    )
    count_before = Request.select().count()  # type: ignore[attr-defined]

    await service.retry_failed_request(user.telegram_user_id, failed.id)

    assert Request.select().count() == count_before + 1  # type: ignore[attr-defined]
    original = Request.get_by_id(failed.id)  # type: ignore[attr-defined]
    assert original.status == "error", "original row must stay in error state"
    retry_row = Request.select().where(Request.correlation_id == "cid-new-row-retry-1").first()  # type: ignore[attr-defined]
    assert retry_row is not None
    assert retry_row.id != failed.id
    assert retry_row.status == "pending"


@pytest.mark.asyncio
async def test_retry_does_not_carry_dedupe_hash(db, user_factory) -> None:
    """Cloned retry row must have dedupe_hash=None so it never collides with the original."""
    user = user_factory(username="retry-no-hash", telegram_user_id=7002)
    service = build_request_service(db)
    failed = Request.create(  # type: ignore[attr-defined]
        user_id=user.telegram_user_id,
        input_url="https://no-hash.example.com",
        normalized_url="https://no-hash.example.com",
        dedupe_hash="hash-no-hash",
        status="error",
        correlation_id="cid-no-hash",
        type="url",
    )

    await service.retry_failed_request(user.telegram_user_id, failed.id)

    retry_row = Request.select().where(Request.correlation_id == "cid-no-hash-retry-1").first()  # type: ignore[attr-defined]
    assert retry_row is not None
    assert retry_row.dedupe_hash is None
