from __future__ import annotations

import asyncio
import time
from typing import Any

from .models import StageError


class BackgroundRequestExecutor:
    def __init__(
        self,
        *,
        logger: Any,
        db_override_factory: Any,
        lock_manager: Any,
        request_repo_for_db: Any,
        has_existing_summary: Any,
        mark_status: Any,
        progress_publisher: Any,
        url_handler: Any,
        forward_handler: Any,
        failure_handler: Any,
        error_payload_builder: Any,
    ) -> None:
        self._logger = logger
        self._db_override_factory = db_override_factory
        self._lock_manager = lock_manager
        self._request_repo_for_db = request_repo_for_db
        self._has_existing_summary = has_existing_summary
        self._mark_status = mark_status
        self._progress_publisher = progress_publisher
        self._url_handler = url_handler
        self._forward_handler = forward_handler
        self._failure_handler = failure_handler
        self._error_payload_builder = error_payload_builder

    async def execute(
        self,
        *,
        request_id: int,
        correlation_id: str | None,
        db_path: str | None,
    ) -> None:
        started_at = time.perf_counter()
        processor_db, processor = self._db_override_factory.resolve(db_path)
        request: dict[str, Any] | None = None
        lock_handle = await self._lock_manager.acquire(request_id, correlation_id)
        if lock_handle is None:
            return

        try:
            repo = self._request_repo_for_db(processor_db)
            request = await repo.async_get_request_by_id(request_id)
            if not request:
                self._logger.error(
                    "bg_request_not_found",
                    extra={"request_id": request_id, "correlation_id": correlation_id},
                )
                return

            if await self._has_existing_summary(processor_db, request_id):
                self._logger.info(
                    "bg_request_already_summarized",
                    extra={
                        "request_id": request_id,
                        "correlation_id": request.get("correlation_id") or correlation_id,
                    },
                )
                await self._progress_publisher.publish(
                    request_id,
                    "COMPLETED",
                    "DONE",
                    "Already summarized",
                    1.0,
                    correlation_id=request.get("correlation_id") or correlation_id,
                )
                return

            cid = request.get("correlation_id") or correlation_id or f"bg-proc-{request_id}"
            await self._mark_status(processor_db, request_id, "processing", cid)
            await self._progress_publisher.publish(
                request_id,
                "PROCESSING",
                "QUEUED",
                "Processing started",
                0.0,
                correlation_id=cid,
            )

            self._logger.info(
                "bg_processing_start",
                extra={
                    "correlation_id": cid,
                    "request_id": request_id,
                    "type": request.get("type"),
                    "url": request.get("input_url"),
                },
            )

            if request.get("type") == "url":
                await self._url_handler.process(
                    request_id=request_id,
                    request=request,
                    db=processor_db,
                    url_processor=processor,
                    correlation_id=cid,
                )
            elif request.get("type") == "forward":
                await self._forward_handler.process(
                    request_id=request_id,
                    request=request,
                    db=processor_db,
                    url_processor=processor,
                    correlation_id=cid,
                )
            else:
                raise StageError(
                    "validation", ValueError(f"Unknown request type: {request.get('type')}")
                )

            await self._mark_status(processor_db, request_id, "success", cid)
            await self._progress_publisher.publish(
                request_id,
                "COMPLETED",
                "DONE",
                "Processing completed",
                1.0,
                correlation_id=cid,
            )
            self._logger.info(
                "bg_processing_success",
                extra={
                    "correlation_id": cid,
                    "request_id": request_id,
                    "type": request.get("type"),
                },
            )
        except StageError as exc:
            await self._failure_handler.handle_stage_error(
                request_id=request_id,
                correlation_id=correlation_id,
                processor_db=processor_db,
                request=request,
                stage_error=exc,
                started_at=started_at,
                error_payload_builder=self._error_payload_builder,
            )
        except asyncio.CancelledError:
            await self._failure_handler.handle_cancelled(
                request_id=request_id,
                correlation_id=correlation_id,
                processor_db=processor_db,
                request=request,
            )
            raise
        except Exception as exc:
            await self._failure_handler.handle_unexpected_error(
                request_id=request_id,
                correlation_id=correlation_id,
                processor_db=processor_db,
                request=request,
                exc=exc,
                started_at=started_at,
                error_payload_builder=self._error_payload_builder,
            )
        finally:
            await self._lock_manager.release(lock_handle)
