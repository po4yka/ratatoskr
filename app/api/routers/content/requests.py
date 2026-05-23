"""Request submission and status endpoints."""

from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, Request

from app.api.exceptions import DuplicateResourceError, ResourceNotFoundError, ValidationError
from app.api.models.requests import SubmitForwardRequest, SubmitURLRequest
from app.api.models.responses import (
    DuplicateDetectionResponse,
    ProcessingStage,
    RequestDetailCrawlResult,
    RequestDetailLlmCall,
    RequestDetailRequest,
    RequestDetailResponse,
    RequestDetailSummary,
    RequestDetailSuccessResponse,
    RequestStatus as PublicRequestStatus,
    RequestStatusData,
    RequestStatusSuccessResponse,
    RetryRequestResponse,
    RetryRequestSuccessResponse,
    SubmitRequestData,
    SubmitRequestResponse,
    SubmitRequestSuccessResponse,
    success_response,
)
from app.api.routers.auth import get_current_user
from app.application.dto.request_lifecycle import public_request_status
from app.application.services.request_service import RequestService
from app.core.time_utils import UTC
from app.di.api import resolve_api_runtime
from app.domain.exceptions.domain_exceptions import (
    DuplicateResourceError as DomainDuplicateResourceError,
    ResourceNotFoundError as DomainResourceNotFoundError,
    ValidationError as DomainValidationError,
)

router = APIRouter()


def _get_request_service(request: Request) -> RequestService:
    """Resolve the shared request workflow service from API runtime."""
    with contextlib.suppress(RuntimeError):
        return cast("RequestService", resolve_api_runtime(request).request_service)

    from app.api.dependencies.database import (
        get_crawl_result_repository,
        get_llm_repository,
        get_request_repository,
        get_session_manager,
        get_summary_repository,
    )

    db = get_session_manager(request)
    return RequestService(
        db=db,
        request_repository=get_request_repository(db, request),
        summary_repository=get_summary_repository(db, request),
        crawl_result_repository=get_crawl_result_repository(db, request),
        llm_repository=get_llm_repository(db, request),
    )


async def _enqueue_request_processing(
    request: Request, request_id: int, correlation_id: str | None
) -> None:
    runtime = resolve_api_runtime(request)
    await runtime.durable_request_queue.enqueue(
        request_id=request_id,
        correlation_id=correlation_id,
    )


def _raise_api_exception(exc: Exception) -> None:
    if isinstance(exc, DomainResourceNotFoundError):
        resource_id = exc.details.get("request_id") or exc.details.get("id") or "unknown"
        raise ResourceNotFoundError("Request", resource_id) from exc
    if isinstance(exc, DomainDuplicateResourceError):
        raise DuplicateResourceError(
            exc.message, existing_id=exc.details.get("existing_id")
        ) from exc
    if isinstance(exc, DomainValidationError):
        raise ValidationError(exc.message, details=exc.details) from exc
    raise exc


@router.post("", response_model=SubmitRequestSuccessResponse)
async def submit_request(
    request: Request,
    request_data: SubmitURLRequest | SubmitForwardRequest,
    user: dict[str, Any] = Depends(get_current_user),
    request_service: RequestService = Depends(_get_request_service),
) -> dict[str, Any]:
    """Submit a new URL or forwarded message for background processing."""
    if isinstance(request_data, SubmitURLRequest):
        input_url = str(request_data.input_url)
        duplicate = await request_service.check_duplicate_url(user["user_id"], input_url)
        if duplicate is not None:
            return success_response(
                DuplicateDetectionResponse(
                    is_duplicate=True,
                    existing_request_id=duplicate.existing_request_id,
                    existing_summary_id=duplicate.existing_summary_id,
                    message="This URL was already summarized",
                    summarized_at=duplicate.summarized_at,
                )
            )

        try:
            created = await request_service.create_url_request(
                user_id=user["user_id"],
                input_url=input_url,
                lang_preference=request_data.lang_preference,
            )
        except Exception as exc:
            if isinstance(exc, DomainDuplicateResourceError):
                return success_response(
                    DuplicateDetectionResponse(
                        is_duplicate=True,
                        existing_request_id=exc.details.get("existing_id"),
                        message=exc.message,
                    )
                )
            _raise_api_exception(exc)

        await _enqueue_request_processing(request, created.id, created.correlation_id)
        return success_response(
            SubmitRequestData(
                request=SubmitRequestResponse(
                    request_id=created.id,
                    correlation_id=created.correlation_id,
                    type=cast("Literal['url', 'forward']", created.type),
                    status=PublicRequestStatus(public_request_status(created.status)),
                    legacy_status=created.status,
                    estimated_wait_seconds=15,
                    created_at=created.created_at.isoformat().replace("+00:00", "Z"),
                    is_duplicate=False,
                )
            )
        )

    try:
        created = await request_service.create_forward_request(
            user_id=user["user_id"],
            content_text=request_data.content_text,
            from_chat_id=request_data.forward_metadata.from_chat_id,
            from_message_id=request_data.forward_metadata.from_message_id,
            lang_preference=request_data.lang_preference,
        )
    except Exception as exc:
        _raise_api_exception(exc)

    await _enqueue_request_processing(request, created.id, created.correlation_id)
    return success_response(
        SubmitRequestData(
            request=SubmitRequestResponse(
                request_id=created.id,
                correlation_id=created.correlation_id,
                type=cast("Literal['url', 'forward']", created.type),
                status=PublicRequestStatus(public_request_status(created.status)),
                legacy_status=created.status,
                estimated_wait_seconds=10,
                created_at=created.created_at.isoformat().replace("+00:00", "Z"),
                is_duplicate=False,
            )
        )
    )


@router.get("/{request_id}", response_model=RequestDetailSuccessResponse)
async def get_request(
    request_id: int,
    user: dict[str, Any] = Depends(get_current_user),
    request_service: RequestService = Depends(_get_request_service),
) -> dict[str, Any]:
    """Get details about a specific request."""
    try:
        details = await request_service.get_request_by_id(user["user_id"], request_id)
    except Exception as exc:
        _raise_api_exception(exc)

    return success_response(
        RequestDetailResponse(
            request=RequestDetailRequest(
                id=details.request.id,
                type=details.request.type,
                status=PublicRequestStatus(public_request_status(details.request.status)),
                legacy_status=details.request.status,
                correlation_id=details.request.correlation_id,
                input_url=details.request.input_url,
                normalized_url=details.request.normalized_url,
                dedupe_hash=details.request.dedupe_hash,
                created_at=details.request.created_at.isoformat().replace("+00:00", "Z"),
                lang_detected=details.request.lang_detected,
            ),
            crawl_result=(
                RequestDetailCrawlResult(
                    status=details.crawl_result.status,
                    http_status=details.crawl_result.http_status,
                    latency_ms=details.crawl_result.latency_ms,
                    error=details.crawl_result.error_text,
                )
                if details.crawl_result
                else None
            ),
            llm_calls=[
                RequestDetailLlmCall(
                    id=item.id,
                    model=item.model,
                    status=item.status,
                    tokens_prompt=item.tokens_prompt,
                    tokens_completion=item.tokens_completion,
                    cost_usd=item.cost_usd,
                    latency_ms=item.latency_ms,
                    created_at=(
                        item.created_at.isoformat().replace("+00:00", "Z")
                        if item.created_at
                        else datetime.now(UTC).isoformat().replace("+00:00", "Z")
                    ),
                )
                for item in details.llm_calls
            ],
            summary=(
                RequestDetailSummary(
                    id=details.summary.id,
                    status="success",
                    created_at=(
                        details.summary.created_at.isoformat().replace("+00:00", "Z")
                        if details.summary.created_at
                        else datetime.now(UTC).isoformat().replace("+00:00", "Z")
                    ),
                )
                if details.summary
                else None
            ),
        )
    )


@router.get("/{request_id}/status", response_model=RequestStatusSuccessResponse)
async def get_request_status(
    request_id: int,
    user: dict[str, Any] = Depends(get_current_user),
    request_service: RequestService = Depends(_get_request_service),
) -> dict[str, Any]:
    """Poll for real-time processing status."""
    try:
        status_info = await request_service.get_request_status(user["user_id"], request_id)
    except Exception as exc:
        _raise_api_exception(exc)

    error_details = status_info.error_details
    return success_response(
        RequestStatusData(
            request_id=status_info.request_id,
            status=PublicRequestStatus(status_info.status),
            legacy_status=status_info.legacy_status,
            stage=ProcessingStage(status_info.stage),
            progress=status_info.progress,
            estimated_seconds_remaining=status_info.estimated_seconds_remaining,
            queue_position=status_info.queue_position,
            error_stage=error_details.stage if error_details else None,
            error_type=error_details.error_type if error_details else None,
            error_message=error_details.error_message if error_details else None,
            error_reason_code=error_details.error_reason_code if error_details else None,
            retryable=error_details.retryable if error_details else None,
            can_retry=status_info.can_retry,
            correlation_id=status_info.correlation_id,
            updated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )
    )


@router.post("/{request_id}/retry", response_model=RetryRequestSuccessResponse)
async def retry_request(
    request_id: int,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    request_service: RequestService = Depends(_get_request_service),
) -> dict[str, Any]:
    """Retry a failed request and enqueue the new request for processing."""
    try:
        created = await request_service.retry_failed_request(user["user_id"], request_id)
    except Exception as exc:
        _raise_api_exception(exc)

    await _enqueue_request_processing(request, created.id, created.correlation_id)
    return success_response(
        RetryRequestResponse(
            new_request_id=created.id,
            correlation_id=created.correlation_id,
            status=PublicRequestStatus(public_request_status(created.status)),
            legacy_status=created.status,
            created_at=created.created_at.isoformat().replace("+00:00", "Z"),
        )
    )
