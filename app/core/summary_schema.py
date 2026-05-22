"""Pydantic schema model for the summary JSON output (field definitions and validators)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.logging_utils import get_logger
from app.core.summary_contract_impl.field_names import FIELD_NAME_MAPPING
from app.core.summary_text_utils import (
    cap_text,
    hash_tagify as _hash_tagify,
)

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Helper utilities used by validators.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Pydantic sub-models
# ---------------------------------------------------------------------------


class Entities(BaseModel):
    people: list[str] = Field(default_factory=list)
    organizations: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)


class Readability(BaseModel):
    method: str = Field(default="Flesch-Kincaid")
    score: float = 0.0
    level: str = Field(default="Unknown")


class KeyStat(BaseModel):
    label: str
    value: float
    unit: str | None = None
    source_excerpt: str | None = None


class Metadata(BaseModel):
    title: str | None = None
    canonical_url: str | None = None
    domain: str | None = None
    author: str | None = None
    published_at: str | None = None
    last_updated: str | None = None


class ExtractiveQuote(BaseModel):
    text: str
    source_span: str | None = None


class QuestionAnswer(BaseModel):
    question: str
    answer: str


class InsightFact(BaseModel):
    fact: str
    why_it_matters: str | None = None
    source_hint: str | None = None
    confidence: float | str | None = None


class Insights(BaseModel):
    topic_overview: str = Field(default="")
    new_facts: list[InsightFact] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    suggested_sources: list[str] = Field(default_factory=list)
    expansion_topics: list[str] = Field(default_factory=list)
    next_exploration: list[str] = Field(default_factory=list)
    caution: str | None = None
    critique: list[str] = Field(default_factory=list)


class TopicTaxonomy(BaseModel):
    label: str
    score: float = 0.0
    path: str | None = None


class ForwardedPostExtras(BaseModel):
    channel_id: int | None = None
    channel_title: str | None = None
    channel_username: str | None = None
    message_id: int | None = None
    post_datetime: str | None = None
    hashtags: list[str] = Field(default_factory=list)
    mentions: list[str] = Field(default_factory=list)


class SemanticChunk(BaseModel):
    text: str
    local_summary: str | None = None
    local_keywords: list[str] = Field(default_factory=list)
    article_id: str | None = None
    section: str | None = None
    language: str | None = None
    topics: list[str] = Field(default_factory=list)


class SourceType(StrEnum):
    """Content type classification."""

    NEWS = "news"
    BLOG = "blog"
    RESEARCH = "research"
    OPINION = "opinion"
    TUTORIAL = "tutorial"
    REFERENCE = "reference"
    PDF = "pdf"
    UNKNOWN = "unknown"


class TemporalFreshness(StrEnum):
    """Content timeliness classification."""

    BREAKING = "breaking"
    RECENT = "recent"
    EVERGREEN = "evergreen"
    UNKNOWN = "unknown"


class HallucinationRisk(StrEnum):
    """Factual uncertainty classification."""

    LOW = "low"
    MED = "med"
    HIGH = "high"
    UNKNOWN = "unknown"


class QualityAssessment(BaseModel):
    author_bias: str | None = None
    emotional_tone: str | None = None
    missing_perspectives: list[str] = Field(default_factory=list)
    evidence_quality: str | None = None
    prompt_injection_suspected: bool = False


# ---------------------------------------------------------------------------
# Main summary model
# ---------------------------------------------------------------------------


class SummaryModel(BaseModel):
    summary_250: str = Field(min_length=1, max_length=250)
    summary_1000: str = Field(min_length=1, max_length=1000)
    tldr: str = Field(min_length=1)
    tldr_ru: str = Field(
        default="",
        description="Full Russian translation of the tldr field. Must be written entirely in Russian (Cyrillic script).",
    )
    key_ideas: list[str] = Field(default_factory=list)
    topic_tags: list[str] = Field(default_factory=list)
    entities: Entities = Field(default_factory=Entities)
    estimated_reading_time_min: int = 0
    key_stats: list[KeyStat] = Field(default_factory=list)
    answered_questions: list[str] = Field(default_factory=list)
    readability: Readability = Field(default_factory=Readability)
    seo_keywords: list[str] = Field(default_factory=list)
    query_expansion_keywords: list[str] = Field(default_factory=list)
    semantic_boosters: list[str] = Field(default_factory=list)
    semantic_chunks: list[SemanticChunk] = Field(default_factory=list)
    article_id: str | None = None

    # Classification fields
    source_type: SourceType = Field(default=SourceType.BLOG)
    temporal_freshness: TemporalFreshness = Field(default=TemporalFreshness.EVERGREEN)

    # New fields
    metadata: Metadata = Field(default_factory=Metadata)
    extractive_quotes: list[ExtractiveQuote] = Field(default_factory=list)
    highlights: list[str] = Field(default_factory=list)
    questions_answered: list[QuestionAnswer] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    topic_taxonomy: list[TopicTaxonomy] = Field(default_factory=list)
    hallucination_risk: HallucinationRisk = Field(default=HallucinationRisk.UNKNOWN)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    forwarded_post_extras: ForwardedPostExtras | None = None
    key_points_to_remember: list[str] = Field(default_factory=list)
    insights: Insights = Field(default_factory=Insights)
    quality: QualityAssessment = Field(default_factory=QualityAssessment)

    # ------------------------------------------------------------------
    # model_validator: normalize field names and backfill summaries
    # ------------------------------------------------------------------

    @model_validator(mode="before")
    @classmethod
    def normalize_and_backfill(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        # --- 1. Normalize field names (camelCase -> snake_case) ---
        normalized: dict[str, Any] = {}
        for key, value in data.items():
            normalized_key = FIELD_NAME_MAPPING.get(key, key)
            normalized[normalized_key] = value
        data = normalized

        # --- 2. Backfill summary fields ---
        tldr = str(data.get("tldr") or "").strip()
        s250 = str(data.get("summary_250") or "").strip()
        s1000 = str(data.get("summary_1000") or "").strip()

        if not s1000 and "summary" in data:
            s1000 = str(data.get("summary") or "").strip()

        if not tldr and s1000:
            tldr = s1000
        if not s1000 and tldr:
            s1000 = tldr
        if not s250 and s1000:
            s250 = cap_text(s1000, 250)
        if not s250 and tldr:
            s250 = cap_text(tldr, 250)
        if not s1000 and s250:
            s1000 = s250
        if not tldr:
            tldr = s1000 or s250

        data["summary_250"] = s250
        data["summary_1000"] = s1000
        data["tldr"] = tldr

        # --- 3. Backfill tldr_ru for Russian sources ---
        tldr_ru = str(data.get("tldr_ru") or "").strip()
        if not tldr_ru and tldr:
            import re as _re

            if _re.search(r"[\u0400-\u04FF]", tldr):
                tldr_ru = tldr
        data["tldr_ru"] = tldr_ru

        return data

    # ------------------------------------------------------------------
    # field_validators
    # ------------------------------------------------------------------

    @field_validator("summary_250", mode="before")
    @classmethod
    def cap_summary_250(cls, v: Any) -> str:
        text = str(v).strip() if v is not None else ""
        return cap_text(text, 250)

    @field_validator("summary_1000", mode="before")
    @classmethod
    def cap_summary_1000(cls, v: Any) -> str:
        text = str(v).strip() if v is not None else ""
        return cap_text(text, 1000)

    @field_validator("topic_tags", mode="before")
    @classmethod
    def normalize_topic_tags(cls, v: Any) -> list[str]:
        if not isinstance(v, list):
            return []
        return _hash_tagify([str(x) for x in v])

    @field_validator("estimated_reading_time_min", mode="before")
    @classmethod
    def coerce_reading_time(cls, v: Any) -> int:
        if v is None:
            return 0
        try:
            return int(v)
        except (ValueError, TypeError):
            return 0

    @field_validator("key_stats", mode="before")
    @classmethod
    def filter_key_stats(cls, v: Any) -> list[dict[str, Any]]:
        if not isinstance(v, list):
            return []
        result: list[dict[str, Any]] = []
        for item in v:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label", "")).strip()
            if not label:
                continue
            try:
                value = float(item.get("value"))
            except (TypeError, ValueError):
                logger.debug("invalid_key_stat_value_skipped", extra={"value": item.get("value")})
                continue
            unit = item.get("unit")
            source_excerpt = item.get("source_excerpt")
            result.append(
                {
                    "label": label,
                    "value": value,
                    "unit": str(unit) if unit is not None else None,
                    "source_excerpt": str(source_excerpt) if source_excerpt is not None else None,
                }
            )
        return result

    @field_validator("answered_questions", mode="before")
    @classmethod
    def coerce_answered_questions(cls, v: Any) -> list[str]:
        if not isinstance(v, list):
            return []
        result = []
        for item in v:
            if isinstance(item, str):
                s = item.strip()
                if s:
                    result.append(s)
            elif isinstance(item, dict):
                # LLMs sometimes return {question, answer} dicts — extract the answer text
                text = item.get("answer") or item.get("question") or ""
                s = str(text).strip()
                if s:
                    result.append(s)
        return result

    @field_validator("hallucination_risk", mode="before")
    @classmethod
    def constrain_hallucination_risk(cls, v: Any) -> str:
        if v is None or str(v).strip() == "":
            logger.warning("summary_hallucination_risk_missing")
            return "unknown"
        val = str(v).strip().lower()
        if val == "medium":
            return "med"
        if val in {"low", "med", "high", "unknown"}:
            return val
        logger.warning("summary_hallucination_risk_invalid", extra={"value": str(v)})
        return "unknown"

    @field_validator("confidence", mode="before")
    @classmethod
    def clamp_confidence(cls, v: Any) -> float:
        if v is None or str(v).strip() == "":
            logger.warning("summary_confidence_missing")
            return 0.0
        try:
            val = float(v)
        except (TypeError, ValueError):
            logger.warning("summary_confidence_invalid", extra={"value": str(v)})
            return 0.0
        if val < 0.0 or val > 1.0:
            logger.warning("summary_confidence_invalid", extra={"value": str(v)})
        return max(0.0, min(1.0, val))

    @field_validator("source_type", mode="before")
    @classmethod
    def default_source_type(cls, v: Any) -> str:
        valid = {m.value for m in SourceType}
        val = str(v).strip().lower() if v is not None else SourceType.UNKNOWN
        if val in valid:
            return val
        logger.warning("summary_source_type_invalid", extra={"value": str(v)})
        return SourceType.UNKNOWN

    @field_validator("temporal_freshness", mode="before")
    @classmethod
    def default_temporal_freshness(cls, v: Any) -> str:
        valid = {m.value for m in TemporalFreshness}
        val = str(v).strip().lower() if v is not None else TemporalFreshness.UNKNOWN
        if val in valid:
            return val
        logger.warning("summary_temporal_freshness_invalid", extra={"value": str(v)})
        return TemporalFreshness.UNKNOWN
