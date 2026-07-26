"""Application service for request submission, inspection, and retry flows."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.application.dto.request_lifecycle import project_request_lifecycle
from app.application.dto.request_workflow import (
    CrawlResultDTO,
    DuplicateRequestMatchDTO,
    RequestCreatedDTO,
    RequestDetailsDTO,
    RequestErrorDetailsDTO,
    RequestLLMCallDTO,
    RequestStatusDTO,
    SummaryRecordDTO,
)
from app.core.logging_utils import get_logger
from app.core.time_utils import UTC
from app.core.url_utils import compute_dedupe_hash, normalize_url
from app.domain.exceptions.domain_exceptions import (
    DuplicateResourceError,
    ResourceNotFoundError,
    ValidationError,
)
from app.domain.models.request import RequestStatus

if TYPE_CHECKING:
    from app.application.ports.requests import (
        CrawlResultRepositoryPort,
        LLMRepositoryPort,
        RequestRepositoryPort,
    )
    from app.application.ports.summaries import SummaryRepositoryPort

logger = get_logger(__name__)


class RequestService:
    """Orchestrates request workflows against application-layer ports."""

    def __init__(
        self,
        *,
        db: Any | None,
        request_repository: RequestRepositoryPort,
        summary_repository: SummaryRepositoryPort,
        crawl_result_repository: CrawlResultRepositoryPort,
        llm_repository: LLMRepositoryPort,
        progress_event_repository: Any | None = None,
    ) -> None:
        self._db = db
        self._request_repo = request_repository
        self._summary_repo = summary_repository
        self._crawl_repo = crawl_result_repository
        self._llm_repo = llm_repository
        self._progress_event_repo = progress_event_repository

    async def check_duplicate_url(
        self,
        user_id: int,
        url: str,
    ) -> DuplicateRequestMatchDTO | None:
        """Return duplicate request metadata for a normalized URL, if present."""
        normalized = self._normalize_url(url)
        existing = await self._request_repo.async_get_request_by_dedupe_hash(
            compute_dedupe_hash(normalized)
        )
        if not existing or existing.get("user_id") != user_id:
            return None

        summary = await self._summary_repo.async_get_summary_by_request(existing["id"])
        return DuplicateRequestMatchDTO(
            existing_request_id=existing["id"],
            existing_summary_id=summary.get("id") if summary else None,
            summarized_at=self._iso_timestamp(existing.get("created_at")),
        )

    async def create_url_request(
        self,
        user_id: int,
        input_url: str,
        lang_preference: str = "auto",
        correlation_id: str | None = None,
    ) -> RequestCreatedDTO:
        """Create a new URL-backed request, enforcing per-user deduplication."""
        normalized = self._normalize_url(input_url)
        duplicate = await self.check_duplicate_url(user_id, input_url)
        if duplicate is not None:
            raise DuplicateResourceError(
                "This URL was already summarized",
                details={"existing_id": duplicate.existing_request_id},
            )

        resolved_correlation_id = (
            correlation_id or f"api-{user_id}-{int(datetime.now(UTC).timestamp())}"
        )
        request_id, created_new = await self._request_repo.async_create_request_once(
            type_="url",
            status=RequestStatus.PENDING,
            correlation_id=resolved_correlation_id,
            user_id=user_id,
            input_url=input_url,
            normalized_url=normalized,
            dedupe_hash=compute_dedupe_hash(normalized),
            lang_detected=lang_preference,
        )
        if not created_new:
            existing = await self._request_repo.async_get_request_by_id(request_id)
            if existing is not None and existing.get("user_id") == user_id:
                raise DuplicateResourceError(
                    "This URL was already submitted",
                    details={"existing_id": request_id},
                )
            raise DuplicateResourceError("This URL is already registered")
        created = await self._request_repo.async_get_request_by_id(request_id)
        if created is None:
            raise ResourceNotFoundError("Request creation failed")

        logger.info(
            "url_request_created",
            extra={
                "request_id": request_id,
                "user_id": user_id,
                "correlation_id": resolved_correlation_id,
            },
        )
        return self._to_request_created(created)

    async def mark_enqueue_failed(
        self,
        *,
        user_id: int,
        request_id: int,
        error_message: str,
    ) -> None:
        """Mark a user-owned request terminal when durable enqueue fails."""
        request = await self._request_repo.async_get_request_by_id(request_id)
        if request is None or request.get("user_id") != user_id:
            raise ResourceNotFoundError("Request", details={"request_id": request_id})
        await self._request_repo.async_update_request_error(
            request_id,
            RequestStatus.ERROR.value,
            error_type="enqueue_failed",
            error_message=error_message,
        )

    async def create_forward_request(
        self,
        user_id: int,
        content_text: str,
        from_chat_id: int,
        from_message_id: int,
        lang_preference: str = "auto",
    ) -> RequestCreatedDTO:
        """Create a new forwarded-message request."""
        correlation_id = f"api-{user_id}-{int(datetime.now(UTC).timestamp())}"
        request_id = await self._request_repo.async_create_request(
            type_="forward",
            status=RequestStatus.PENDING,
            correlation_id=correlation_id,
            user_id=user_id,
            content_text=content_text,
            fwd_from_chat_id=from_chat_id,
            fwd_from_msg_id=from_message_id,
            lang_detected=lang_preference,
        )
        created = await self._request_repo.async_get_request_by_id(request_id)
        if created is None:
            raise ResourceNotFoundError("Request creation failed")

        logger.info(
            "forward_request_created",
            extra={"request_id": request_id, "user_id": user_id, "correlation_id": correlation_id},
        )
        return self._to_request_created(created)

    async def get_request_by_id(self, user_id: int, request_id: int) -> RequestDetailsDTO:
        """Return full request details for an authorized user."""
        context = await self._request_repo.async_get_request_context(request_id)
        request = (
            context.get("request")
            if context
            else await self._request_repo.async_get_request_by_id(request_id)
        )
        if not request or request.get("user_id") != user_id:
            raise ResourceNotFoundError("Request", details={"request_id": request_id})

        crawl_result = (
            context.get("crawl_result")
            if context
            else await self._crawl_repo.async_get_crawl_result_by_request(request_id)
        )
        summary = (
            context.get("summary")
            if context
            else await self._summary_repo.async_get_summary_by_request(request_id)
        )
        llm_calls = await self._llm_repo.async_get_llm_calls_by_request(request_id)

        return RequestDetailsDTO(
            request=self._to_request_created(request),
            crawl_result=self._to_crawl_result(crawl_result),
            llm_calls=[self._to_llm_call(item) for item in llm_calls],
            summary=self._to_summary_record(summary),
        )

    async def get_request_status(self, user_id: int, request_id: int) -> RequestStatusDTO:
        """Return a polling-friendly status projection for a request."""
        context = await self._request_repo.async_get_request_context(request_id)
        request = (
            context.get("request")
            if context
            else await self._request_repo.async_get_request_by_id(request_id)
        )
        if not request or request.get("user_id") != user_id:
            raise ResourceNotFoundError("Request", details={"request_id": request_id})

        legacy_status = str(request.get("status") or "")
        stage = "queued"
        progress: dict[str, Any] | None = None
        queue_position: int | None = None
        error_details: RequestErrorDetailsDTO | None = None
        can_retry = False
        effective_status = legacy_status
        latest_progress = (
            await self._progress_event_repo.get_latest(request_id)
            if self._progress_event_repo is not None
            else None
        )

        if latest_progress is not None:
            stage = latest_progress.stage or "queued"
            if latest_progress.status:
                effective_status = latest_progress.status
            if latest_progress.progress is not None:
                progress = {
                    "percentage": round(latest_progress.progress * 100),
                    "value": latest_progress.progress,
                }
            if latest_progress.status in {"failed", "cancelled"} or latest_progress.kind == "error":
                error_details = RequestErrorDetailsDTO(
                    stage=latest_progress.stage,
                    error_type=(latest_progress.payload or {}).get("error")
                    or (latest_progress.payload or {}).get("error_code"),
                    error_message=latest_progress.message or "Request failed",
                    error_reason_code=(latest_progress.payload or {}).get("error_code"),
                    retryable=True,
                    debug={
                        "event_id": latest_progress.event_id,
                        "sequence": latest_progress.sequence,
                    },
                )
                can_retry = True
        elif legacy_status == "processing":
            crawl_result = (
                context.get("crawl_result")
                if context
                else await self._crawl_repo.async_get_crawl_result_by_request(request_id)
            )
            summary = (
                context.get("summary")
                if context
                else await self._summary_repo.async_get_summary_by_request(request_id)
            )
            llm_call_count = await self._llm_repo.async_count_llm_calls_by_request(request_id)

            if not crawl_result:
                stage = "extracting"
                progress = {"current_step": 1, "total_steps": 3, "percentage": 33}
            elif llm_call_count == 0 or not summary:
                stage = "summarizing"
                progress = {"current_step": 2, "total_steps": 3, "percentage": 66}
            else:
                stage = "persisting"
                progress = {"current_step": 3, "total_steps": 3, "percentage": 90}
        elif legacy_status == "pending":
            stage = "queued"
            created_at = self._coerce_datetime(request.get("created_at"))
            if created_at is not None:
                queue_position = (
                    await self._request_repo.async_count_pending_requests_before(created_at)
                ) + 1
        elif legacy_status in {"success", "complete", "ok", "completed"}:
            stage = "done"
        elif legacy_status in {"error", "failed"}:
            stage = "done"
            error_details = await self._derive_error_details(request_id)
            if error_details is None:
                error_details = RequestErrorDetailsDTO(
                    stage=None,
                    error_type=None,
                    error_message="Request failed",
                    error_reason_code=None,
                    retryable=False,
                    debug=None,
                )
            elif not error_details.error_message:
                error_details = RequestErrorDetailsDTO(
                    stage=error_details.stage,
                    error_type=error_details.error_type,
                    error_message="Request failed",
                    error_reason_code=error_details.error_reason_code,
                    retryable=error_details.retryable,
                    debug=error_details.debug,
                )
            can_retry = True
        elif legacy_status == "cancelled":
            stage = "done"
            error_details = RequestErrorDetailsDTO(
                stage="cancelled",
                error_type="REQUEST_CANCELLED",
                error_message="Request was cancelled",
                error_reason_code="REQUEST_CANCELLED",
                retryable=True,
                debug=None,
            )
            can_retry = True

        lifecycle = project_request_lifecycle(status=effective_status, stage=stage)
        return RequestStatusDTO(
            request_id=request_id,
            status=lifecycle.status,
            legacy_status=legacy_status or None,
            stage=lifecycle.stage,
            progress=progress,
            estimated_seconds_remaining=8
            if stage in {"extracting", "summarizing", "persisting"}
            else None,
            queue_position=queue_position,
            error_details=error_details,
            can_retry=can_retry,
            correlation_id=request.get("correlation_id"),
        )

    async def update_request_content_text(
        self,
        *,
        user_id: int,
        request_id: int,
        content_text: str,
    ) -> None:
        """Persist selected content text for an owned request."""
        request = await self._request_repo.async_get_request_by_id(request_id)
        if not request or request.get("user_id") != user_id:
            raise ResourceNotFoundError("Request", details={"request_id": request_id})

        await self._request_repo.async_update_request_content_text(request_id, content_text)

    async def retry_failed_request(self, user_id: int, request_id: int) -> RequestCreatedDTO:
        """Create a new pending request by cloning a failed request."""
        original = await self._request_repo.async_get_request_by_id(request_id)
        if not original or original.get("user_id") != user_id:
            raise ResourceNotFoundError("Request", details={"request_id": request_id})
        if original.get("status") != "error":
            raise ValidationError("Only failed requests can be retried")

        correlation_id = f"{original.get('correlation_id')}-retry-1"
        new_request_id = await self._request_repo.async_create_request(
            type_=original.get("type", "url"),
            status=RequestStatus.PENDING,
            correlation_id=correlation_id,
            user_id=user_id,
            input_url=original.get("input_url"),
            normalized_url=original.get("normalized_url"),
            content_text=original.get("content_text"),
            fwd_from_chat_id=original.get("fwd_from_chat_id"),
            fwd_from_msg_id=original.get("fwd_from_msg_id"),
            lang_detected=original.get("lang_detected"),
            # Mark the first LLM call for this retry request as "user_retry".
            initial_attempt_trigger="user_retry",
        )
        created = await self._request_repo.async_get_request_by_id(new_request_id)
        if created is None:
            raise ResourceNotFoundError("Request creation failed")

        logger.info(
            "retry_request_created",
            extra={
                "new_request_id": new_request_id,
                "original_request_id": request_id,
                "user_id": user_id,
            },
        )
        return self._to_request_created(created)

    async def _derive_error_details(self, request_id: int) -> RequestErrorDetailsDTO | None:
        """Derive the most useful error details across request, LLM, and crawl state."""
        request_context = await self._request_repo.async_get_request_error_context(request_id)
        if request_context:
            debug = {
                "pipeline": request_context.get("pipeline"),
                "component": request_context.get("component"),
                "attempt": request_context.get("attempt"),
                "max_attempts": request_context.get("max_attempts"),
                "timestamp": request_context.get("timestamp"),
            }
            return RequestErrorDetailsDTO(
                stage=request_context.get("stage") or "unknown",
                error_type=request_context.get("error_type") or request_context.get("reason_code"),
                error_message=request_context.get("error_message") or "Request failed",
                error_reason_code=request_context.get("reason_code"),
                retryable=bool(request_context.get("retryable", True)),
                debug={key: value for key, value in debug.items() if value is not None} or None,
            )

        latest_llm = await self._llm_repo.async_get_latest_error_by_request(request_id)
        if latest_llm:
            error_context = latest_llm.get("error_context_json") or {}
            error_type = None
            reason_code = "LLM_FAILED"
            message = latest_llm.get("error_text")
            if isinstance(error_context, dict):
                error_type = error_context.get("status_code") or error_context.get("error_code")
                reason_code = (
                    error_context.get("api_error") or error_context.get("error_code") or reason_code
                )
                if not message:
                    message = error_context.get("message")

            return RequestErrorDetailsDTO(
                stage="llm_summarization",
                error_type=error_type or reason_code,
                error_message=message or "LLM summarization failed",
                error_reason_code=reason_code,
                retryable=True,
                debug=None,
            )

        crawl_result = await self._crawl_repo.async_get_crawl_result_by_request(request_id)
        if crawl_result and (
            crawl_result.get("status") == "error" or crawl_result.get("error_text")
        ):
            reason_code = crawl_result.get("firecrawl_error_code") or "EXTRACTION_FAILED"
            return RequestErrorDetailsDTO(
                stage="content_extraction",
                error_type=reason_code,
                error_message=(
                    crawl_result.get("error_text")
                    or crawl_result.get("firecrawl_error_message")
                    or "Content extraction failed"
                ),
                error_reason_code=reason_code,
                retryable=True,
                debug=None,
            )

        return None

    @staticmethod
    def _normalize_url(url: str) -> str:
        try:
            return normalize_url(url)
        except ValueError as exc:
            raise ValidationError(f"Invalid URL: {exc}") from exc

    @staticmethod
    def _iso_timestamp(value: Any) -> str:
        if isinstance(value, datetime):
            return value.isoformat().replace("+00:00", "Z")
        return str(value or "")

    @staticmethod
    def _coerce_datetime(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str) and value:
            normalized = value.replace("Z", "+00:00")
            with_timezone = normalized if "T" in normalized else normalized.replace(" ", "T")
            try:
                return datetime.fromisoformat(with_timezone)
            except ValueError:
                return None
        return None

    @staticmethod
    def _to_request_created(row: dict[str, Any]) -> RequestCreatedDTO:
        created_at = RequestService._coerce_datetime(row.get("created_at"))
        if created_at is None:
            created_at = datetime.now(UTC)
        return RequestCreatedDTO(
            id=row["id"],
            type=row.get("type", "url"),
            status=str(row.get("status", "pending")),
            correlation_id=row.get("correlation_id"),
            created_at=created_at,
            input_url=row.get("input_url"),
            normalized_url=row.get("normalized_url"),
            dedupe_hash=row.get("dedupe_hash"),
            lang_detected=row.get("lang_detected"),
            content_text=row.get("content_text"),
            fwd_from_chat_id=row.get("fwd_from_chat_id"),
            fwd_from_msg_id=row.get("fwd_from_msg_id"),
        )

    @staticmethod
    def _to_crawl_result(row: dict[str, Any] | None) -> CrawlResultDTO | None:
        if row is None:
            return None
        return CrawlResultDTO(
            status=row.get("status"),
            http_status=row.get("http_status"),
            latency_ms=row.get("latency_ms"),
            error_text=row.get("error_text"),
            source_url=row.get("source_url"),
        )

    @staticmethod
    def _to_llm_call(row: dict[str, Any]) -> RequestLLMCallDTO:
        return RequestLLMCallDTO(
            id=row["id"],
            model=row.get("model"),
            status=row.get("status"),
            tokens_prompt=row.get("tokens_prompt"),
            tokens_completion=row.get("tokens_completion"),
            cost_usd=row.get("cost_usd"),
            latency_ms=row.get("latency_ms"),
            created_at=row.get("created_at"),
        )

    @staticmethod
    def _to_summary_record(row: dict[str, Any] | None) -> SummaryRecordDTO | None:
        if row is None:
            return None
        payload = row.get("json_payload")
        return SummaryRecordDTO(
            id=row["id"],
            lang=row.get("lang"),
            created_at=row.get("created_at"),
            json_payload=payload if isinstance(payload, dict) else {},
        )
