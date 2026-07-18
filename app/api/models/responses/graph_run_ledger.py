"""Privacy-safe graph run ledger response models."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003  # used at runtime by Pydantic schema generation

from pydantic import BaseModel, ConfigDict, Field

from app.api.models.responses.common import MetaInfo


class _GraphRunModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


class GraphRunLedgerChronologyEntry(_GraphRunModel):
    """A structural graph event with user content deliberately removed."""

    sequence: int
    kind: str
    stage: str | None = None
    status: str | None = None
    occurred_at: datetime = Field(validation_alias="occurredAt", serialization_alias="occurredAt")


class GraphRunLedgerAttempt(_GraphRunModel):
    """Sanitized LLM attempt telemetry without prompt, response, or error text."""

    attempt_index: int = Field(validation_alias="attemptIndex", serialization_alias="attemptIndex")
    trigger: str
    provider: str | None = None
    model: str | None = None
    status: str | None = None
    latency_ms: int | None = Field(
        default=None, validation_alias="latencyMs", serialization_alias="latencyMs"
    )
    total_latency_ms: int | None = Field(
        default=None, validation_alias="totalLatencyMs", serialization_alias="totalLatencyMs"
    )
    prompt_tokens: int | None = Field(
        default=None, validation_alias="promptTokens", serialization_alias="promptTokens"
    )
    completion_tokens: int | None = Field(
        default=None,
        validation_alias="completionTokens",
        serialization_alias="completionTokens",
    )
    cost_usd: float | None = Field(
        default=None, validation_alias="costUsd", serialization_alias="costUsd"
    )
    fallback_model: str | None = Field(
        default=None, validation_alias="fallbackModel", serialization_alias="fallbackModel"
    )
    retry_exhausted: bool = Field(
        validation_alias="retryExhausted", serialization_alias="retryExhausted"
    )
    error_present: bool = Field(validation_alias="errorPresent", serialization_alias="errorPresent")


class GraphRunLedgerMetrics(_GraphRunModel):
    node_count: int = Field(validation_alias="nodeCount", serialization_alias="nodeCount")
    attempt_count: int = Field(validation_alias="attemptCount", serialization_alias="attemptCount")
    repair_count: int = Field(validation_alias="repairCount", serialization_alias="repairCount")
    fallback_count: int = Field(
        validation_alias="fallbackCount", serialization_alias="fallbackCount"
    )
    graph_latency_ms: int | None = Field(
        default=None, validation_alias="graphLatencyMs", serialization_alias="graphLatencyMs"
    )
    llm_latency_ms: int = Field(validation_alias="llmLatencyMs", serialization_alias="llmLatencyMs")
    prompt_tokens: int = Field(validation_alias="promptTokens", serialization_alias="promptTokens")
    completion_tokens: int = Field(
        validation_alias="completionTokens", serialization_alias="completionTokens"
    )
    total_cost_usd: float = Field(
        validation_alias="totalCostUsd", serialization_alias="totalCostUsd"
    )


class GraphRunLedgerFeedback(_GraphRunModel):
    """Aggregate feedback labels; free-form feedback text is never exposed."""

    feedback_count: int = Field(
        validation_alias="feedbackCount", serialization_alias="feedbackCount"
    )
    rating_average: float | None = Field(
        default=None, validation_alias="ratingAverage", serialization_alias="ratingAverage"
    )
    issue_count: int = Field(validation_alias="issueCount", serialization_alias="issueCount")
    latest_feedback_at: datetime | None = Field(
        default=None,
        validation_alias="latestFeedbackAt",
        serialization_alias="latestFeedbackAt",
    )


class GraphRunLedgerResponse(_GraphRunModel):
    request_id: int = Field(validation_alias="requestId", serialization_alias="requestId")
    request_status: str = Field(
        validation_alias="requestStatus", serialization_alias="requestStatus"
    )
    created_at: datetime = Field(validation_alias="createdAt", serialization_alias="createdAt")
    chronology: list[GraphRunLedgerChronologyEntry] = Field(default_factory=list)
    attempts: list[GraphRunLedgerAttempt] = Field(default_factory=list)
    metrics: GraphRunLedgerMetrics
    feedback: GraphRunLedgerFeedback


class GraphRunLedgerSuccessResponse(BaseModel):
    success: bool = True
    data: GraphRunLedgerResponse
    meta: MetaInfo = Field(default_factory=MetaInfo)


class GraphRunEvaluationListResponse(BaseModel):
    """Bounded, sanitized records suitable for offline prompt/model evaluation."""

    items: list[GraphRunLedgerResponse] = Field(default_factory=list)
    limit: int


class GraphRunEvaluationListSuccessResponse(BaseModel):
    success: bool = True
    data: GraphRunEvaluationListResponse
    meta: MetaInfo = Field(default_factory=MetaInfo)
