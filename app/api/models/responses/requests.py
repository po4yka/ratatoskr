"""Request API response models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .common import ProcessingStage, RequestStatus, SuccessResponse


class RequestStatusData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    request_id: int = Field(validation_alias="requestId", serialization_alias="requestId")
    status: RequestStatus
    legacy_status: str | None = Field(
        default=None, validation_alias="legacyStatus", serialization_alias="legacyStatus"
    )
    stage: ProcessingStage
    progress: dict[str, Any] | None = None
    estimated_seconds_remaining: int | None = Field(
        default=None,
        validation_alias="estimatedSecondsRemaining",
        serialization_alias="estimatedSecondsRemaining",
    )
    queue_position: int | None = Field(
        default=None, validation_alias="queuePosition", serialization_alias="queuePosition"
    )
    error_stage: str | None = Field(
        default=None, validation_alias="errorStage", serialization_alias="errorStage"
    )
    error_type: str | None = Field(
        default=None, validation_alias="errorType", serialization_alias="errorType"
    )
    error_message: str | None = Field(
        default=None, validation_alias="errorMessage", serialization_alias="errorMessage"
    )
    error_reason_code: str | None = Field(
        default=None, validation_alias="errorReasonCode", serialization_alias="errorReasonCode"
    )
    retryable: bool | None = Field(
        default=None, validation_alias="retryable", serialization_alias="retryable"
    )
    can_retry: bool = Field(
        default=False, validation_alias="canRetry", serialization_alias="canRetry"
    )
    correlation_id: str | None = Field(
        default=None, validation_alias="correlationId", serialization_alias="correlationId"
    )
    updated_at: str = Field(validation_alias="updatedAt", serialization_alias="updatedAt")


class ProgressEventData(BaseModel):
    event_id: str = Field(serialization_alias="eventId")
    request_id: int = Field(serialization_alias="requestId")
    sequence: int
    kind: str
    stage: str | None = None
    status: str | None = None
    message: str | None = None
    progress: float | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(serialization_alias="createdAt")
    correlation_id: str | None = Field(default=None, serialization_alias="correlationId")


class SubmitRequestResponse(BaseModel):
    request_id: int = Field(serialization_alias="requestId")
    correlation_id: str = Field(serialization_alias="correlationId")
    type: Literal["url", "forward"]
    status: RequestStatus
    legacy_status: str | None = Field(default=None, serialization_alias="legacyStatus")
    estimated_wait_seconds: int = Field(serialization_alias="estimatedWaitSeconds")
    created_at: str = Field(serialization_alias="createdAt")
    is_duplicate: bool = Field(default=False, serialization_alias="isDuplicate")


class SubmitRequestData(BaseModel):
    request: SubmitRequestResponse


class DuplicateCheckData(BaseModel):
    is_duplicate: bool = Field(serialization_alias="isDuplicate")
    normalized_url: str | None = Field(default=None, serialization_alias="normalizedUrl")
    dedupe_hash: str | None = Field(default=None, serialization_alias="dedupeHash")
    request_id: int | None = Field(default=None, serialization_alias="requestId")
    summary_id: int | None = Field(default=None, serialization_alias="summaryId")
    summarized_at: str | None = Field(default=None, serialization_alias="summarizedAt")
    summary: dict[str, Any] | None = None


class DuplicateDetectionResponse(BaseModel):
    is_duplicate: bool = Field(serialization_alias="isDuplicate")
    existing_request_id: int | None = Field(default=None, serialization_alias="existingRequestId")
    existing_summary_id: int | None = Field(default=None, serialization_alias="existingSummaryId")
    message: str
    summarized_at: str | None = Field(default=None, serialization_alias="summarizedAt")


class RequestDetailCrawlResult(BaseModel):
    status: str | None = None
    http_status: int | None = Field(default=None, serialization_alias="httpStatus")
    latency_ms: int | None = Field(default=None, serialization_alias="latencyMs")
    error: str | None = None


class RequestDetailLlmCall(BaseModel):
    id: int
    model: str | None = None
    status: str | None = None
    tokens_prompt: int | None = Field(default=None, serialization_alias="tokensPrompt")
    tokens_completion: int | None = Field(default=None, serialization_alias="tokensCompletion")
    cost_usd: float | None = Field(default=None, serialization_alias="costUsd")
    latency_ms: int | None = Field(default=None, serialization_alias="latencyMs")
    created_at: str = Field(serialization_alias="createdAt")


class RequestDetailSummary(BaseModel):
    id: int
    status: str
    created_at: str = Field(serialization_alias="createdAt")


class RequestDetailRequest(BaseModel):
    id: int
    type: str
    status: RequestStatus
    legacy_status: str | None = Field(default=None, serialization_alias="legacyStatus")
    correlation_id: str = Field(serialization_alias="correlationId")
    input_url: str | None = Field(default=None, serialization_alias="inputUrl")
    normalized_url: str | None = Field(default=None, serialization_alias="normalizedUrl")
    dedupe_hash: str | None = Field(default=None, serialization_alias="dedupeHash")
    created_at: str = Field(serialization_alias="createdAt")
    lang_detected: str | None = Field(default=None, serialization_alias="langDetected")


class RequestDetailResponse(BaseModel):
    request: RequestDetailRequest
    crawl_result: RequestDetailCrawlResult | None = Field(
        default=None, serialization_alias="crawlResult"
    )
    llm_calls: list[RequestDetailLlmCall] = Field(
        default_factory=list, serialization_alias="llmCalls"
    )
    summary: RequestDetailSummary | None = None


class RetryRequestResponse(BaseModel):
    new_request_id: int = Field(serialization_alias="newRequestId")
    correlation_id: str = Field(serialization_alias="correlationId")
    status: RequestStatus
    legacy_status: str | None = Field(default=None, serialization_alias="legacyStatus")
    created_at: str = Field(serialization_alias="createdAt")


class SubmitRequestSuccessResponse(SuccessResponse):
    data: SubmitRequestData | DuplicateDetectionResponse


class RequestStatusSuccessResponse(SuccessResponse):
    data: RequestStatusData


class RequestDetailSuccessResponse(SuccessResponse):
    data: RequestDetailResponse


class RetryRequestSuccessResponse(SuccessResponse):
    data: RetryRequestResponse
