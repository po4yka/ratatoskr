from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from app.observability.failure_observability import (
    REASON_UNKNOWN_EXTRACTION_FAILURE,
    persist_request_failure,
)

if TYPE_CHECKING:
    from .models import StageError


class BackgroundFailureHandler:
    def __init__(
        self,
        *,
        logger: Any,
        retry_policy: Any,
        request_repo_for_db: Any,
        mark_status: Any,
        progress_publisher: Any,
    ) -> None:
        self._logger = logger
        self._retry_policy = retry_policy
        self._request_repo_for_db = request_repo_for_db
        self._mark_status = mark_status
        self._progress_publisher = progress_publisher

    async def handle_stage_error(
        self,
        *,
        request_id: int,
        correlation_id: str | None,
        processor_db: Any,
        request: dict[str, Any] | None,
        stage_error: StageError,
        started_at: float,
        error_payload_builder: Any,
    ) -> None:
        error_payload = error_payload_builder(stage_error.stage, stage_error.original)
        cid = (request or {}).get("correlation_id") or correlation_id
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        target_repo = self._request_repo_for_db(processor_db)
        await persist_request_failure(
            request_repo=target_repo,
            logger=self._logger,
            request_id=request_id,
            correlation_id=cid,
            stage=stage_error.stage,
            component="background_processor",
            reason_code=error_payload["error_code"],
            error=stage_error.original,
            retryable=True,
            attempt=self._retry_policy.attempts,
            max_attempts=self._retry_policy.attempts,
            processing_time_ms=elapsed_ms,
        )
        if request:
            await self._mark_status(processor_db, request_id, "error", cid)
        await self._progress_publisher.publish(
            request_id,
            "FAILED",
            stage_error.stage.upper(),
            str(stage_error.original),
            0.0,
            error=str(stage_error.original),
            correlation_id=cid,
        )
        self._logger.error(
            "bg_processing_failed",
            exc_info=True,
            extra={"correlation_id": cid, "request_id": request_id, **error_payload},
        )

    async def handle_cancelled(
        self,
        *,
        request_id: int,
        correlation_id: str | None,
        processor_db: Any,
        request: dict[str, Any] | None,
    ) -> None:
        self._logger.warning(
            "bg_processing_cancelled",
            extra={"correlation_id": correlation_id, "request_id": request_id},
        )
        if request:
            await self._mark_status(
                processor_db,
                request_id,
                "cancelled",
                correlation_id or request.get("correlation_id"),
            )
        await self._progress_publisher.publish(
            request_id,
            "CANCELLED",
            "CANCELLED",
            "Task cancelled",
            0.0,
            correlation_id=correlation_id or (request or {}).get("correlation_id"),
        )

    async def handle_unexpected_error(
        self,
        *,
        request_id: int,
        correlation_id: str | None,
        processor_db: Any,
        request: dict[str, Any] | None,
        exc: Exception,
        started_at: float,
        error_payload_builder: Any,
    ) -> None:
        error_payload = error_payload_builder("unknown", exc)
        cid = (request or {}).get("correlation_id") or correlation_id
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        target_repo = self._request_repo_for_db(processor_db)
        await persist_request_failure(
            request_repo=target_repo,
            logger=self._logger,
            request_id=request_id,
            correlation_id=cid,
            stage="unknown",
            component="background_processor",
            reason_code=REASON_UNKNOWN_EXTRACTION_FAILURE,
            error=exc,
            retryable=True,
            attempt=self._retry_policy.attempts,
            max_attempts=self._retry_policy.attempts,
            processing_time_ms=elapsed_ms,
        )
        if request:
            await self._mark_status(processor_db, request_id, "error", cid)
        await self._progress_publisher.publish(
            request_id,
            "FAILED",
            "UNKNOWN",
            str(exc),
            0.0,
            error=str(exc),
            correlation_id=cid,
        )
        self._logger.error(
            "bg_processing_failed",
            exc_info=True,
            extra={"correlation_id": cid, "request_id": request_id, **error_payload},
        )
