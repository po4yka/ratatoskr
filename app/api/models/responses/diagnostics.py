"""Owner-only diagnostics response models."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003  # used at runtime by Pydantic schema generation
from typing import Literal

from pydantic import BaseModel, Field

from app.api.models.responses.common import MetaInfo

HealthStatus = Literal["healthy", "degraded", "unhealthy", "disabled", "unavailable", "unknown"]


class DiagnosticsComponent(BaseModel):
    status: HealthStatus = "unknown"
    failure_count: int = 0
    last_error_code: str | None = None
    last_error_message: str | None = None
    last_failure_at: datetime | None = None
    checked_at: datetime | None = None
    cached: bool = True
    details: dict[str, object] = Field(default_factory=dict)


class DiagnosticsProviderStatus(BaseModel):
    provider: str
    status: HealthStatus = "unknown"
    total_count: int = 0
    failure_count: int = 0
    last_error_code: str | None = None
    last_error_message: str | None = None
    last_failure_at: datetime | None = None


class DiagnosticsQueueBacklog(BaseModel):
    by_status: dict[str, int] = Field(default_factory=dict)
    runnable_count: int = 0
    oldest_queued_at: datetime | None = None
    oldest_retry_after: datetime | None = None
    expired_running_leases: int = 0


class DiagnosticsVectorIndexLag(BaseModel):
    status: HealthStatus = "unknown"
    missing_embeddings: int = 0
    stale_embeddings: int = 0
    pending_embeddings: int = 0
    expected_summaries: int = 0
    expected_repositories: int = 0
    indexed_points: int | None = None
    indexed_summaries: int | None = None
    indexed_repositories: int | None = None
    missing_summary_vectors: int = 0
    missing_repository_vectors: int = 0
    stale_embedding_model_count: int = 0
    lag_seconds: float = 0.0
    vector_store_available: bool = False
    oldest_unindexed_summary_updated_at: datetime | None = None
    latest_indexed_at: datetime | None = None
    details: dict[str, object] = Field(default_factory=dict)


class DiagnosticsSyncFailure(BaseModel):
    source: Literal["rss", "github", "source", "request", "import", "vector"]
    event_id: str
    correlation_id: str | None = None
    error_code: str | None = None
    message: str | None = None
    occurred_at: datetime | None = None
    retryable: bool | None = None
    details: dict[str, object] = Field(default_factory=dict)


class DiagnosticsStorageGrowth(BaseModel):
    database_size_mb: float | None = None
    table_counts: dict[str, int] = Field(default_factory=dict)
    created_last_24h: dict[str, int] = Field(default_factory=dict)
    created_last_7d: dict[str, int] = Field(default_factory=dict)


class DiagnosticsResponse(BaseModel):
    generated_at: datetime
    cache_ttl_seconds: int
    components: dict[str, DiagnosticsComponent]
    scraper_providers: list[DiagnosticsProviderStatus]
    llm_providers: list[DiagnosticsProviderStatus]
    queue_backlog: DiagnosticsQueueBacklog
    vector_indexing_lag: DiagnosticsVectorIndexLag
    latest_sync_failures: list[DiagnosticsSyncFailure]
    storage_growth: DiagnosticsStorageGrowth


class DiagnosticsSuccessResponse(BaseModel):
    success: bool = True
    data: DiagnosticsResponse
    meta: MetaInfo = Field(default_factory=MetaInfo)
