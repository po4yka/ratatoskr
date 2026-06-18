# ruff: noqa: TC001
"""Summary and search API response models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .aggregation import AggregationSourceBundle
from .common import PaginationInfo, SuccessResponse


class SummaryCompact(BaseModel):
    id: int = Field(description="Unique summary identifier")
    request_id: int = Field(serialization_alias="requestId", description="Associated request ID")
    title: str = Field(description="Article title")
    domain: str = Field(description="Source domain (e.g., example.com)")
    url: str = Field(description="Original article URL")
    tldr: str = Field(description="Concise multi-sentence summary")
    summary_250: str = Field(
        serialization_alias="summary250", description="Short summary (<=250 chars)"
    )
    reading_time_min: int = Field(
        serialization_alias="readingTimeMin", description="Estimated reading time in minutes"
    )
    topic_tags: list[str] = Field(serialization_alias="topicTags", description="Topic hashtags")
    is_read: bool = Field(serialization_alias="isRead", description="User read status")
    is_favorited: bool = Field(
        default=False, serialization_alias="isFavorited", description="User favorite status"
    )
    lang: Literal["en", "ru", "auto"] = Field(description="Detected or preferred language")
    created_at: str = Field(
        serialization_alias="createdAt", description="ISO 8601 creation timestamp"
    )
    confidence: float = Field(description="LLM confidence score (0.0-1.0)")
    hallucination_risk: Literal["low", "medium", "high", "unknown"] = Field(
        serialization_alias="hallucinationRisk", description="Assessed hallucination risk level"
    )
    image_url: str | None = Field(default=None, serialization_alias="imageUrl")
    source_coverage: Literal[
        "full",
        "partial",
        "abstract_only",
        "transcript_missing",
        "unknown",
    ] = Field(default="unknown", serialization_alias="sourceCoverage")
    repair_attempted: bool = Field(default=False, serialization_alias="repairAttempted")
    repair_succeeded: bool = Field(default=False, serialization_alias="repairSucceeded")
    prompt_injection_suspected: bool = Field(
        default=False, serialization_alias="promptInjectionSuspected"
    )
    validation_warning_count: int = Field(default=0, serialization_alias="validationWarningCount")


class SummaryDetailEntities(BaseModel):
    people: list[str] = Field(default_factory=list)
    organizations: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)


class SummaryDetailReadability(BaseModel):
    method: str
    score: float
    level: str


class SummaryDetailKeyStat(BaseModel):
    label: str
    value: float
    unit: str | None = None
    source_excerpt: str | None = Field(default=None, serialization_alias="sourceExcerpt")


class SummaryDetailSummary(BaseModel):
    summary_250: str = Field(serialization_alias="summary250")
    summary_1000: str = Field(serialization_alias="summary1000")
    tldr: str
    key_ideas: list[str] = Field(serialization_alias="keyIdeas")
    topic_tags: list[str] = Field(serialization_alias="topicTags")
    entities: SummaryDetailEntities
    estimated_reading_time_min: int = Field(serialization_alias="estimatedReadingTimeMin")
    key_stats: list[SummaryDetailKeyStat] = Field(
        default_factory=list, serialization_alias="keyStats"
    )
    answered_questions: list[str] = Field(
        default_factory=list, serialization_alias="answeredQuestions"
    )
    readability: SummaryDetailReadability | None = None
    seo_keywords: list[str] = Field(default_factory=list, serialization_alias="seoKeywords")


class SummaryDetailRequest(BaseModel):
    id: str
    type: str
    url: str | None = None
    normalized_url: str | None = Field(default=None, serialization_alias="normalizedUrl")
    dedupe_hash: str | None = Field(default=None, serialization_alias="dedupeHash")
    status: str
    lang_detected: str | None = Field(default=None, serialization_alias="langDetected")
    created_at: str = Field(serialization_alias="createdAt")
    updated_at: str = Field(serialization_alias="updatedAt")


class SummaryDetailSource(BaseModel):
    url: str | None = None
    title: str | None = None
    domain: str | None = None
    author: str | None = None
    published_at: str | None = Field(default=None, serialization_alias="publishedAt")
    word_count: int | None = Field(default=None, serialization_alias="wordCount")
    content_type: str | None = Field(default=None, serialization_alias="contentType")


class SummaryDetailQuality(BaseModel):
    validation_warnings: list[str] = Field(
        default_factory=list, serialization_alias="validationWarnings"
    )
    repair_attempted: bool = Field(default=False, serialization_alias="repairAttempted")
    repair_succeeded: bool = Field(default=False, serialization_alias="repairSucceeded")
    structured_output_mode: str | None = Field(
        default=None, serialization_alias="structuredOutputMode"
    )
    model_used: str | None = Field(default=None, serialization_alias="modelUsed")
    source_coverage: Literal[
        "full",
        "partial",
        "abstract_only",
        "transcript_missing",
        "unknown",
    ] = Field(default="unknown", serialization_alias="sourceCoverage")
    extraction_quality: str | None = Field(default=None, serialization_alias="extractionQuality")
    extraction_confidence: float | None = Field(
        default=None, serialization_alias="extractionConfidence"
    )
    prompt_injection_suspected: bool = Field(
        default=False, serialization_alias="promptInjectionSuspected"
    )


class SummaryDetailProcessing(BaseModel):
    model_used: str | None = Field(default=None, serialization_alias="modelUsed")
    tokens_used: int | None = Field(default=None, serialization_alias="tokensUsed")
    processing_time_ms: int | None = Field(default=None, serialization_alias="processingTimeMs")
    crawl_time_ms: int | None = Field(default=None, serialization_alias="crawlTimeMs")
    confidence: float | None = None
    hallucination_risk: Literal["low", "medium", "high", "unknown"] | None = Field(
        default=None, serialization_alias="hallucinationRisk"
    )
    quality: SummaryDetailQuality | None = None


class SummaryDetail(BaseModel):
    summary: SummaryDetailSummary
    request: SummaryDetailRequest
    source: SummaryDetailSource
    processing: SummaryDetailProcessing
    source_bundle: AggregationSourceBundle | None = Field(
        default=None, serialization_alias="sourceBundle"
    )
    reading_progress: float | None = Field(default=None, serialization_alias="readingProgress")
    last_read_offset: int | None = Field(default=None, serialization_alias="lastReadOffset")


class SummaryContent(BaseModel):
    summary_id: int = Field(serialization_alias="summaryId")
    request_id: int | None = Field(default=None, serialization_alias="requestId")
    format: Literal["markdown", "text", "html"]
    content: str
    content_type: Literal["text/markdown", "text/plain", "text/html"] = Field(
        serialization_alias="contentType"
    )
    lang: Literal["en", "ru", "auto"] | None = None
    source_url: str | None = Field(default=None, serialization_alias="sourceUrl")
    title: str | None = None
    domain: str | None = None
    retrieved_at: str = Field(serialization_alias="retrievedAt")
    size_bytes: int | None = Field(default=None, serialization_alias="sizeBytes")
    checksum_sha256: str | None = Field(default=None, serialization_alias="checksumSha256")


class SummaryContentData(BaseModel):
    content: SummaryContent


class SummaryListStats(BaseModel):
    total_summaries: int = Field(serialization_alias="totalSummaries")
    unread_count: int = Field(serialization_alias="unreadCount")


class SummaryListResponse(BaseModel):
    summaries: list[SummaryCompact]
    pagination: PaginationInfo
    stats: SummaryListStats


class SummaryRecommendationsResponse(BaseModel):
    recommendations: list[SummaryCompact]
    reason: str
    count: int


class SearchResult(BaseModel):
    request_id: int = Field(serialization_alias="requestId")
    summary_id: int = Field(serialization_alias="summaryId")
    url: str | None
    title: str
    domain: str | None = None
    snippet: str | None = None
    tldr: str | None = None
    published_at: str | None = Field(default=None, serialization_alias="publishedAt")
    created_at: str = Field(serialization_alias="createdAt")
    relevance_score: float | None = Field(default=None, serialization_alias="relevanceScore")
    topic_tags: list[str] | None = Field(default=None, serialization_alias="topicTags")
    is_read: bool | None = Field(default=None, serialization_alias="isRead")
    match_signals: list[str] | None = Field(default=None, serialization_alias="matchSignals")
    match_explanation: str | None = Field(default=None, serialization_alias="matchExplanation")
    score_breakdown: dict[str, float] | None = Field(
        default=None, serialization_alias="scoreBreakdown"
    )


class SearchResultsData(BaseModel):
    results: list[SearchResult]
    pagination: PaginationInfo
    query: str
    intent: str | None = None
    mode: str | None = None
    facets: dict[str, Any] | None = None


class UpdateSummaryResponse(BaseModel):
    id: int
    is_read: bool = Field(serialization_alias="isRead")
    updated_at: str = Field(serialization_alias="updatedAt")


class SaveReadingPositionResponse(BaseModel):
    id: int
    progress: float
    last_read_offset: int


class DeleteSummaryResponse(BaseModel):
    id: int
    deleted_at: str = Field(serialization_alias="deletedAt")


class ToggleFavoriteResponse(BaseModel):
    success: bool
    is_favorited: bool = Field(serialization_alias="isFavorited")


class FeedbackResponse(BaseModel):
    id: str
    rating: int | None = None
    issues: list[str] | None = None
    comment: str | None = None
    created_at: str = Field(serialization_alias="createdAt")


class BulkSummaryUpdateResponse(BaseModel):
    updated: int


class SummaryListSuccessResponse(SuccessResponse):
    data: SummaryListResponse


class SummaryRecommendationsSuccessResponse(SuccessResponse):
    data: SummaryRecommendationsResponse


class SummaryDetailSuccessResponse(SuccessResponse):
    data: SummaryDetail


class SummaryContentSuccessResponse(SuccessResponse):
    data: SummaryContentData


class UpdateSummarySuccessResponse(SuccessResponse):
    data: UpdateSummaryResponse


class SaveReadingPositionSuccessResponse(SuccessResponse):
    data: SaveReadingPositionResponse


class DeleteSummarySuccessResponse(SuccessResponse):
    data: DeleteSummaryResponse


class BulkSummaryUpdateSuccessResponse(SuccessResponse):
    data: BulkSummaryUpdateResponse


class ToggleFavoriteSuccessResponse(SuccessResponse):
    data: ToggleFavoriteResponse


class FeedbackSuccessResponse(SuccessResponse):
    data: FeedbackResponse
