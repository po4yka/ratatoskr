"""Typed response payloads for admin, system, and health endpoints."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class AdminUserData(BaseModel):
    user_id: int
    username: str | None
    is_owner: bool
    summary_count: int
    request_count: int
    tag_count: int
    collection_count: int
    created_at: str


class AdminUsersData(BaseModel):
    users: list[AdminUserData]
    total_users: int


class PipelineJobsData(BaseModel):
    pending: int
    processing: int
    completed_today: int
    failed_today: int


class ImportJobsData(BaseModel):
    active: int
    completed_today: int


class AdminJobsData(BaseModel):
    pipeline: PipelineJobsData
    imports: ImportJobsData


class RecentContentFailureData(BaseModel):
    id: int
    url: str | None
    error_type: str | None
    error_message: str | None
    created_at: str


class AdminContentHealthData(BaseModel):
    total_summaries: int
    total_requests: int
    failed_requests: int
    failed_by_error_type: dict[str, int]
    recent_failures: list[RecentContentFailureData]


class LlmMetricsData(BaseModel):
    total_calls: int
    avg_latency_ms: float
    total_prompt_tokens: int
    total_completion_tokens: int
    total_cost_usd: float
    error_rate: float


class ScraperMetricsData(BaseModel):
    total: int
    success: int
    success_rate: float


class DatabaseInfoData(BaseModel):
    file_size_mb: float
    database_size_mb: float
    table_counts: dict[str, int]
    db_path: str


class AdminSystemMetricsData(BaseModel):
    llm_7d: LlmMetricsData
    scraper_7d: dict[str, ScraperMetricsData]
    database: DatabaseInfoData


class LlmCostTotalsData(BaseModel):
    calls: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float


class LlmCostPeriodsData(BaseModel):
    today_cost_usd: float
    month_cost_usd: float


class LlmProviderModelCostData(BaseModel):
    provider: str
    model: str
    status: str
    calls: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    avg_latency_ms: float


class LlmBudgetLimitsData(BaseModel):
    max_tokens_per_request: int | None
    max_cost_usd_per_request: float | None
    daily_soft_budget_usd: float | None
    monthly_soft_budget_usd: float | None
    warning_threshold_ratio: float
    daily_hard_budget_usd: float | None
    monthly_hard_budget_usd: float | None


class LlmBudgetData(BaseModel):
    status: str
    hard_stop: bool
    warning: bool
    reasons: list[str]
    limits: LlmBudgetLimitsData


class AdminLlmCostsData(BaseModel):
    window_start: str
    totals: LlmCostTotalsData
    periods: LlmCostPeriodsData
    by_provider_model: list[LlmProviderModelCostData]
    budget: LlmBudgetData | None = None


class AuditLogEntryData(BaseModel):
    id: int
    timestamp: str
    level: str
    event: str
    details: dict[str, Any] | None


class AdminAuditLogData(BaseModel):
    logs: list[AuditLogEntryData]
    total: int
    limit: int
    offset: int


class CacheClearData(BaseModel):
    cleared_keys: int


class HealthComponentData(BaseModel):
    """Common component fields while preserving provider-specific diagnostics."""

    model_config = ConfigDict(extra="allow")

    status: str
    latency_ms: float | None = None
    error: str | None = None


class HealthComponentsData(BaseModel):
    database: HealthComponentData
    redis: HealthComponentData
    scraper: HealthComponentData
    vector_store: HealthComponentData
    circuit_breakers: dict[str, str]


class DetailedHealthData(BaseModel):
    status: str
    health_score: float
    timestamp: str
    total_latency_ms: float
    components: HealthComponentsData


class ReadinessData(BaseModel):
    ready: bool
    timestamp: str


class LivenessData(BaseModel):
    alive: bool
    timestamp: str


class BasicHealthData(BaseModel):
    status: str
    timestamp: str
