"""Application DTOs for mixed-source aggregation."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.models.source import SourceKind  # noqa: TC001

if TYPE_CHECKING:
    from app.domain.models.source import SourceItem


class ExtractedTextKind(StrEnum):
    """Kinds of extracted text blocks that can compose a normalized source."""

    BODY = "body"
    TITLE = "title"
    CAPTION = "caption"
    TRANSCRIPT = "transcript"
    OCR = "ocr"
    ALT_TEXT = "alt_text"


class SourceMediaKind(StrEnum):
    """Media kinds that can be attached to a normalized source document."""

    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    DOCUMENT = "document"


class SourceSubmissionKind(StrEnum):
    """Kinds of raw bundle submissions accepted by the orchestrator."""

    URL = "url"
    TELEGRAM_MESSAGE = "telegram_message"


class SourceSubmission(BaseModel):
    """Raw bundle item before source classification and extraction."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    submission_kind: SourceSubmissionKind
    url: str | None = None
    telegram_message: Any | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_payload(self) -> SourceSubmission:
        if self.submission_kind == SourceSubmissionKind.URL:
            if not self.url or not self.url.strip():
                msg = "URL source submissions require a non-empty URL"
                raise ValueError(msg)
            return self
        if self.submission_kind == SourceSubmissionKind.TELEGRAM_MESSAGE:
            if self.telegram_message is None:
                msg = "Telegram source submissions require a message payload"
                raise ValueError(msg)
            return self

        msg = f"Unsupported source submission kind: {self.submission_kind}"
        raise ValueError(msg)

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> SourceSubmission:
        return cls(
            submission_kind=SourceSubmissionKind.URL,
            url=url,
            metadata=dict(metadata or {}),
        )

    @classmethod
    def from_telegram_message(
        cls,
        telegram_message: Any,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> SourceSubmission:
        return cls(
            submission_kind=SourceSubmissionKind.TELEGRAM_MESSAGE,
            telegram_message=telegram_message,
            metadata=dict(metadata or {}),
        )

    @classmethod
    def from_telegram_messages(
        cls,
        telegram_messages: list[Any] | tuple[Any, ...],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> SourceSubmission:
        return cls(
            submission_kind=SourceSubmissionKind.TELEGRAM_MESSAGE,
            telegram_message=list(telegram_messages),
            metadata=dict(metadata or {}),
        )


class AggregationFailure(BaseModel):
    """Shared failure payload for bundle-level and item-level errors."""

    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class SourceTextBlock(BaseModel):
    """One extracted text segment with source-aware typing."""

    model_config = ConfigDict(frozen=True)

    kind: ExtractedTextKind = ExtractedTextKind.BODY
    text: str
    position: int | None = None
    confidence: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            msg = "Source text blocks cannot be empty"
            raise ValueError(msg)
        return stripped


class SourceMediaAsset(BaseModel):
    """Normalized media descriptor for multimodal aggregation."""

    model_config = ConfigDict(frozen=True)

    kind: SourceMediaKind
    url: str | None = None
    local_path: str | None = None
    mime_type: str | None = None
    alt_text: str | None = None
    position: int | None = None
    duration_sec: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_locator(self) -> SourceMediaAsset:
        if not (self.url or self.local_path):
            msg = "Source media assets require either a URL or a local path"
            raise ValueError(msg)
        return self


class SourceProvenance(BaseModel):
    """Stable provenance metadata for an extracted source document."""

    model_config = ConfigDict(frozen=True)

    source_item_id: str
    source_kind: SourceKind
    original_value: str | None = None
    normalized_value: str | None = None
    external_id: str | None = None
    request_id: int | None = None
    telegram_chat_id: int | None = None
    telegram_message_id: int | None = None
    telegram_media_group_id: str | None = None
    extraction_source: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class NormalizedSourceDocument(BaseModel):
    """Shared extractor output contract for text, media, and provenance."""

    model_config = ConfigDict(frozen=True)

    source_item_id: str
    source_kind: SourceKind
    title: str | None = None
    text: str = ""
    detected_language: str | None = None
    text_blocks: list[SourceTextBlock] = Field(default_factory=list)
    media: list[SourceMediaAsset] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    provenance: SourceProvenance

    @model_validator(mode="after")
    def validate_content(self) -> NormalizedSourceDocument:
        has_text = bool(self.text.strip()) or any(block.text.strip() for block in self.text_blocks)
        if not has_text and not self.media:
            msg = "Normalized source documents require extracted text or media"
            raise ValueError(msg)
        return self

    @classmethod
    def from_extracted_content(
        cls,
        *,
        source_item: SourceItem,
        text: str = "",
        title: str | None = None,
        detected_language: str | None = None,
        content_source: str | None = None,
        media_urls: list[str] | None = None,
        media_assets: list[SourceMediaAsset] | None = None,
        metadata: dict[str, Any] | None = None,
        text_kind: ExtractedTextKind = ExtractedTextKind.BODY,
    ) -> NormalizedSourceDocument:
        """Build a normalized document from existing extractor output."""

        document_metadata = dict(metadata or {})
        if content_source:
            document_metadata.setdefault("content_source", content_source)

        text_blocks: list[SourceTextBlock] = []
        if title:
            text_blocks.append(
                SourceTextBlock(kind=ExtractedTextKind.TITLE, text=title, position=0)
            )
        if text.strip():
            text_blocks.append(
                SourceTextBlock(
                    kind=text_kind,
                    text=text,
                    position=len(text_blocks),
                )
            )

        if media_assets is not None:
            media = [
                asset.model_copy(update={"position": index})
                for index, asset in enumerate(media_assets)
                if asset.url or asset.local_path
            ]
        else:
            media = [
                SourceMediaAsset(kind=SourceMediaKind.IMAGE, url=url, position=index)
                for index, url in enumerate(media_urls or [])
                if url
            ]
        return cls(
            source_item_id=source_item.stable_id,
            source_kind=source_item.kind,
            title=title,
            text=text.strip(),
            detected_language=detected_language,
            text_blocks=text_blocks,
            media=media,
            metadata=document_metadata,
            provenance=SourceProvenance(
                source_item_id=source_item.stable_id,
                source_kind=source_item.kind,
                original_value=source_item.original_value,
                normalized_value=source_item.normalized_value,
                external_id=source_item.external_id,
                request_id=source_item.request_id,
                telegram_chat_id=source_item.telegram_chat_id,
                telegram_message_id=source_item.telegram_message_id,
                telegram_media_group_id=source_item.telegram_media_group_id,
                extraction_source=content_source,
                metadata=dict(source_item.metadata),
            ),
        )


class SourceExtractionItemResult(BaseModel):
    """Item-level extraction result ready for bundle synthesis."""

    model_config = ConfigDict(frozen=True)

    position: int
    item_id: int
    source_item_id: str
    source_kind: SourceKind
    status: str
    request_id: int | None = None
    duplicate_of_item_id: int | None = None
    normalized_document: NormalizedSourceDocument | None = None
    failure: AggregationFailure | None = None
    extraction_metadata: dict[str, Any] = Field(default_factory=dict)


class AggregationEvidenceKind(StrEnum):
    """Evidence classes used during mixed-source synthesis."""

    TEXT = "text"
    IMAGE = "image"
    OCR = "ocr"
    TRANSCRIPT = "transcript"
    METADATA = "metadata"


class AggregationRelationshipSignal(BaseModel):
    """Optional relationship signal attached to bundle synthesis output."""

    model_config = ConfigDict(frozen=True)

    relationship_type: str
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str | None = None
    signals_used: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_relationship_analysis(cls, relationship: Any) -> AggregationRelationshipSignal:
        """Build a lightweight relationship DTO from the batch-analysis output."""

        relationship_type = getattr(relationship, "relationship_type", "unknown")
        if hasattr(relationship_type, "value"):
            relationship_type = relationship_type.value

        metadata: dict[str, Any] = {}
        series_info = getattr(relationship, "series_info", None)
        if series_info is not None:
            metadata["series_info"] = (
                series_info.model_dump() if hasattr(series_info, "model_dump") else series_info
            )
        cluster_info = getattr(relationship, "cluster_info", None)
        if cluster_info is not None:
            metadata["cluster_info"] = (
                cluster_info.model_dump() if hasattr(cluster_info, "model_dump") else cluster_info
            )

        return cls(
            relationship_type=str(relationship_type or "unknown"),
            confidence=float(getattr(relationship, "confidence", 0.0) or 0.0),
            reasoning=getattr(relationship, "reasoning", None),
            signals_used=list(getattr(relationship, "signals_used", []) or []),
            metadata=metadata,
        )


class AggregationEvidenceWeight(BaseModel):
    """Per-evidence weighting used when synthesizing source documents."""

    model_config = ConfigDict(frozen=True)

    kind: AggregationEvidenceKind
    weight: float = Field(ge=0.0)
    rationale: str | None = None


class AggregationSourceWeight(BaseModel):
    """Weight assigned to one extracted source item."""

    model_config = ConfigDict(frozen=True)

    source_item_id: str
    source_kind: SourceKind
    total_weight: float = Field(ge=0.0)
    evidence_weights: list[AggregationEvidenceWeight] = Field(default_factory=list)
    rationale: str | None = None


class AggregatedClaim(BaseModel):
    """Synthesis claim with explicit provenance back to source items."""

    model_config = ConfigDict(frozen=True)

    claim_id: str
    text: str
    source_item_ids: list[str] = Field(default_factory=list)
    evidence_kinds: list[AggregationEvidenceKind] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("claim_id", "text")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            msg = "Aggregated claims require non-empty text"
            raise ValueError(msg)
        return stripped


class AggregatedContradiction(BaseModel):
    """Potential contradiction or disagreement detected across bundle items."""

    model_config = ConfigDict(frozen=True)

    summary: str
    source_item_ids: list[str] = Field(default_factory=list)
    resolution_note: str | None = None

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            msg = "Contradictions require a non-empty summary"
            raise ValueError(msg)
        return stripped


class DuplicateSignal(BaseModel):
    """Cross-source duplicate or overlap signal detected during synthesis."""

    model_config = ConfigDict(frozen=True)

    summary: str
    source_item_ids: list[str] = Field(default_factory=list)

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            msg = "Duplicate signals require a non-empty summary"
            raise ValueError(msg)
        return stripped


class SourceCoverageEntry(BaseModel):
    """Coverage metadata for one submitted bundle item."""

    model_config = ConfigDict(frozen=True)

    position: int
    item_id: int
    source_item_id: str
    source_kind: SourceKind
    status: str
    used_in_summary: bool = False
    claim_ids: list[str] = Field(default_factory=list)
    contradiction_count: int = 0
    duplicate_signal_count: int = 0
    total_weight: float | None = Field(default=None, ge=0.0)


class MultiSourceExtractionOutput(BaseModel):
    """Bundle extraction output with per-item results."""

    model_config = ConfigDict(frozen=True)

    session_id: int
    correlation_id: str
    status: str
    successful_count: int
    failed_count: int
    duplicate_count: int
    items: list[SourceExtractionItemResult] = Field(default_factory=list)


class MultiSourceAggregationOutput(BaseModel):
    """Bundle-level synthesis output for mixed extracted sources."""

    model_config = ConfigDict(frozen=True)

    session_id: int
    correlation_id: str
    status: str
    source_type: str
    total_items: int
    extracted_items: int
    used_source_count: int
    overview: str
    key_claims: list[AggregatedClaim] = Field(default_factory=list)
    contradictions: list[AggregatedContradiction] = Field(default_factory=list)
    complementary_points: list[str] = Field(default_factory=list)
    duplicate_signals: list[DuplicateSignal] = Field(default_factory=list)
    source_weights: list[AggregationSourceWeight] = Field(default_factory=list)
    source_coverage: list[SourceCoverageEntry] = Field(default_factory=list)
    relationship_signal: AggregationRelationshipSignal | None = None
    entities: list[str] = Field(default_factory=list)
    topic_tags: list[str] = Field(default_factory=list)
    total_estimated_consumption_time_min: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("overview")
    @classmethod
    def validate_overview(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            msg = "Aggregation output requires a non-empty overview"
            raise ValueError(msg)
        return stripped


class MultiSourceExtractionInput(BaseModel):
    """Input bundle for the mixed-source extraction agent.

    Defined here (in the application DTO layer) so that application services
    can construct instances without importing from ``app.agents``.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    correlation_id: str
    user_id: int
    items: list[SourceSubmission]
    allow_partial_success: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)
    progress_callback: Any = None


class MultiSourceAggregationInput(BaseModel):
    """Input contract for mixed-source bundle synthesis.

    Defined here (in the application DTO layer) so that application services
    can construct instances without importing from ``app.agents``.
    """

    model_config = ConfigDict(frozen=True)

    session_id: int
    correlation_id: str
    items: list[SourceExtractionItemResult]
    language: str = "en"
    relationship_signal: AggregationRelationshipSignal | None = None

    @model_validator(mode="after")
    def validate_items(self) -> MultiSourceAggregationInput:
        if not self.items:
            msg = "Multi-source aggregation requires at least one bundle item"
            raise ValueError(msg)
        return self


__all__ = [
    "AggregatedClaim",
    "AggregatedContradiction",
    "AggregationEvidenceKind",
    "AggregationEvidenceWeight",
    "AggregationFailure",
    "AggregationRelationshipSignal",
    "AggregationSourceWeight",
    "DuplicateSignal",
    "ExtractedTextKind",
    "MultiSourceAggregationInput",
    "MultiSourceAggregationOutput",
    "MultiSourceExtractionInput",
    "MultiSourceExtractionOutput",
    "NormalizedSourceDocument",
    "SourceCoverageEntry",
    "SourceExtractionItemResult",
    "SourceMediaAsset",
    "SourceMediaKind",
    "SourceProvenance",
    "SourceSubmission",
    "SourceSubmissionKind",
    "SourceTextBlock",
]
