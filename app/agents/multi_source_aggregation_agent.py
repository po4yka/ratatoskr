"""Mixed-source synthesis agent for extracted aggregation bundles."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from app.agents._aggregation_utils import (
    _EVIDENCE_BASE_WEIGHTS,
    _HASHTAG_RE,
    _NUMBER_RE,
    _SENTENCE_SPLIT_RE,
    _canonical_sentence,
    _clean_string_list,
    _coerce_int,
    _filter_source_item_ids,
    _has_image_evidence,
    _has_metadata_evidence,
    _has_ocr_evidence,
    _has_text_evidence,
    _has_transcript_evidence,
    _normalize_tags,
    _numeric_sentence_base,
    _parse_evidence_kinds,
    _select_metadata,
    _truncate,
)
from app.agents.base_agent import AgentResult, BaseAgent
from app.application.dto.aggregation import (
    AggregatedClaim,
    AggregatedContradiction,
    AggregationEvidenceKind,
    AggregationEvidenceWeight,
    AggregationSourceWeight,
    DuplicateSignal,
    ExtractedTextKind,
    MultiSourceAggregationInput,
    MultiSourceAggregationOutput,
    NormalizedSourceDocument,
    SourceCoverageEntry,
    SourceExtractionItemResult,
)
from app.domain.models.source import AggregationItemStatus, AggregationSessionStatus
from app.prompts.file_cache import read_prompt_text

if TYPE_CHECKING:
    from collections.abc import Iterable

    from app.application.ports.aggregation_sessions import AggregationSessionRepositoryPort
    from app.application.ports.llm_client import LLMClientProtocol

_PROMPT_DIR = Path(__file__).parent.parent / "prompts"


class _SentenceCache:
    """Run-scoped cache for sentence splits derived from immutable DTO content."""

    def __init__(self) -> None:
        self._documents: dict[
            tuple[int, str | None, str, tuple[tuple[int, str, str], ...]], list[str]
        ] = {}
        self._blocks: dict[tuple[int, str | None, int, str, str], list[str]] = {}

    def document_sentences(self, document: NormalizedSourceDocument) -> list[str]:
        key = (
            id(document),
            document.title,
            document.text,
            tuple((id(block), block.kind.value, block.text) for block in document.text_blocks),
        )
        if key not in self._documents:
            sentences: list[str] = []
            if document.title:
                sentences.append(document.title)
            for block in document.text_blocks:
                block_sentences = self._split_text(block.text)
                self._blocks[self._block_key(document, block)] = [
                    *([document.title] if document.title else []),
                    *block_sentences,
                ]
                sentences.extend(block_sentences)
            if not sentences and document.text.strip():
                sentences.extend(self._split_text(document.text))
            self._documents[key] = sentences
        return self._documents[key]

    def block_sentences(
        self,
        document: NormalizedSourceDocument,
        block: Any,
    ) -> list[str]:
        key = self._block_key(document, block)
        if key not in self._blocks:
            sentences: list[str] = []
            if document.title:
                sentences.append(document.title)
            sentences.extend(self._split_text(block.text))
            self._blocks[key] = sentences
        return self._blocks[key]

    @staticmethod
    def _block_key(
        document: NormalizedSourceDocument,
        block: Any,
    ) -> tuple[int, str | None, int, str, str]:
        return (id(document), document.title, id(block), block.kind.value, block.text)

    @staticmethod
    def _split_text(text: str) -> list[str]:
        return [sentence.strip() for sentence in _SENTENCE_SPLIT_RE.split(text) if sentence.strip()]


class _AggregationLLMResponse(BaseModel):
    key_claims: list[Any] = []
    contradictions: list[Any] = []
    duplicate_signals: list[Any] = []
    overview: str = ""
    complementary_points: list[Any] = []
    entities: list[Any] = []
    topic_tags: list[Any] = []


class MultiSourceAggregationAgent(
    BaseAgent[MultiSourceAggregationInput, MultiSourceAggregationOutput]
):
    """Synthesize normalized bundle items into one provenance-aware output."""

    def __init__(
        self,
        *,
        aggregation_session_repo: AggregationSessionRepositoryPort,
        llm_client: LLMClientProtocol | None = None,
        correlation_id: str | None = None,
    ) -> None:
        super().__init__(name="MultiSourceAggregationAgent", correlation_id=correlation_id)
        self._aggregation_session_repo = aggregation_session_repo
        self._llm = llm_client

    async def execute(
        self, input_data: MultiSourceAggregationInput
    ) -> AgentResult[MultiSourceAggregationOutput]:
        """Generate one mixed-source synthesis output from extracted bundle items."""

        self.correlation_id = input_data.correlation_id
        extracted_items = [
            item
            for item in input_data.items
            if item.status == AggregationItemStatus.EXTRACTED.value and item.normalized_document
        ]
        if not extracted_items:
            return AgentResult.error_result(
                "Cannot synthesize bundle without extracted source documents",
                session_id=input_data.session_id,
            )

        self.log_info(
            "multi_source_aggregation_started",
            session_id=input_data.session_id,
            extracted_items=len(extracted_items),
            total_items=len(input_data.items),
        )

        try:
            source_weights = [self._build_source_weight(item) for item in extracted_items]
            weight_by_source_id = {weight.source_item_id: weight for weight in source_weights}
            sentence_cache = _SentenceCache()
            duplicate_signals = self._detect_duplicate_signals(
                extracted_items,
                sentence_cache=sentence_cache,
            )
            contradiction_hints = self._detect_contradiction_hints(
                extracted_items,
                sentence_cache=sentence_cache,
            )

            output, llm_cost_usd = await self._generate_with_llm(
                input_data=input_data,
                extracted_items=extracted_items,
                source_weights=source_weights,
                duplicate_signals=duplicate_signals,
                contradiction_hints=contradiction_hints,
                sentence_cache=sentence_cache,
            )
            if output is None:
                output = self._build_fallback_output(
                    input_data=input_data,
                    extracted_items=extracted_items,
                    source_weights=source_weights,
                    duplicate_signals=duplicate_signals,
                    contradiction_hints=contradiction_hints,
                    sentence_cache=sentence_cache,
                )

            coverage = self._build_source_coverage(
                items=input_data.items,
                output=output,
                weight_by_source_id=weight_by_source_id,
            )
            used_source_count = sum(1 for entry in coverage if entry.used_in_summary)
            source_type = self._resolve_source_type(extracted_items)
            total_estimated_consumption_time_min = self._estimate_consumption_time_minutes(
                extracted_items
            )
            output = output.model_copy(
                update={
                    "source_type": source_type,
                    "used_source_count": used_source_count,
                    "source_coverage": coverage,
                    "total_estimated_consumption_time_min": total_estimated_consumption_time_min,
                }
            )

            await self._aggregation_session_repo.async_update_aggregation_session_output(
                input_data.session_id,
                output.model_dump(mode="json"),
            )
            return AgentResult.success_result(
                output,
                session_id=input_data.session_id,
                used_source_count=used_source_count,
                source_type=source_type,
                llm_cost_usd=llm_cost_usd,
            )
        except Exception as exc:
            self.log_error(
                "multi_source_aggregation_failed",
                session_id=input_data.session_id,
                error=str(exc),
            )
            return AgentResult.error_result(
                f"Aggregation failed: {exc}",
                session_id=input_data.session_id,
            )

    async def _generate_with_llm(
        self,
        *,
        input_data: MultiSourceAggregationInput,
        extracted_items: list[SourceExtractionItemResult],
        source_weights: list[AggregationSourceWeight],
        duplicate_signals: list[DuplicateSignal],
        contradiction_hints: list[AggregatedContradiction],
        sentence_cache: _SentenceCache,
    ) -> tuple[MultiSourceAggregationOutput | None, float]:
        if self._llm is None:
            return None, 0.0

        prompt = self._load_prompt(input_data.language)
        context = self._build_llm_context(
            input_data=input_data,
            extracted_items=extracted_items,
            source_weights=source_weights,
            duplicate_signals=duplicate_signals,
            contradiction_hints=contradiction_hints,
        )
        try:
            result = await self._llm.chat_structured(
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": context},
                ],
                response_model=_AggregationLLMResponse,
                max_retries=3,
                max_tokens=2400,
                temperature=0.2,
                request_id=None,
            )
        except Exception as exc:
            self.log_warning("multi_source_aggregation_llm_failed", error=str(exc))
            return None, 0.0

        try:
            return (
                self._parse_llm_output(
                    parsed=result.parsed.model_dump(),
                    input_data=input_data,
                    extracted_items=extracted_items,
                    source_weights=source_weights,
                    fallback_duplicates=duplicate_signals,
                    fallback_contradictions=contradiction_hints,
                    sentence_cache=sentence_cache,
                ),
                float(result.cost_usd or 0.0),
            )
        except Exception as exc:
            self.log_warning("multi_source_aggregation_llm_parse_failed", error=str(exc))
            return None, float(result.cost_usd or 0.0)

    def _parse_llm_output(
        self,
        *,
        parsed: dict[str, Any],
        input_data: MultiSourceAggregationInput,
        extracted_items: list[SourceExtractionItemResult],
        source_weights: list[AggregationSourceWeight],
        fallback_duplicates: list[DuplicateSignal],
        fallback_contradictions: list[AggregatedContradiction],
        sentence_cache: _SentenceCache | None = None,
    ) -> MultiSourceAggregationOutput:
        valid_source_ids = {
            item.normalized_document.source_item_id
            for item in extracted_items
            if item.normalized_document is not None
        }
        claims = self._parse_claims(parsed.get("key_claims"), valid_source_ids)
        if not claims:
            claims = self._fallback_claims(
                extracted_items,
                source_weights,
                sentence_cache=sentence_cache,
            )

        contradictions = self._parse_contradictions(
            parsed.get("contradictions"),
            valid_source_ids,
        )
        if not contradictions:
            contradictions = fallback_contradictions

        duplicate_signals = self._parse_duplicate_signals(
            parsed.get("duplicate_signals"),
            valid_source_ids,
        )
        if not duplicate_signals:
            duplicate_signals = fallback_duplicates

        overview = str(parsed.get("overview") or "").strip()
        if not overview:
            overview = self._build_overview(extracted_items)

        complementary_points = [
            str(point).strip()
            for point in parsed.get("complementary_points", [])
            if str(point).strip()
        ]
        entities = _clean_string_list(parsed.get("entities"))
        topic_tags = _normalize_tags(parsed.get("topic_tags"))

        return MultiSourceAggregationOutput(
            session_id=input_data.session_id,
            correlation_id=input_data.correlation_id,
            status=self._resolve_output_status(input_data.items),
            source_type=self._resolve_source_type(extracted_items),
            total_items=len(input_data.items),
            extracted_items=len(extracted_items),
            used_source_count=0,
            overview=overview,
            key_claims=claims,
            contradictions=contradictions,
            complementary_points=complementary_points,
            duplicate_signals=duplicate_signals,
            source_weights=source_weights,
            source_coverage=[],
            relationship_signal=input_data.relationship_signal,
            entities=entities,
            topic_tags=topic_tags,
            total_estimated_consumption_time_min=self._estimate_consumption_time_minutes(
                extracted_items
            ),
        )

    def _build_fallback_output(
        self,
        *,
        input_data: MultiSourceAggregationInput,
        extracted_items: list[SourceExtractionItemResult],
        source_weights: list[AggregationSourceWeight],
        duplicate_signals: list[DuplicateSignal],
        contradiction_hints: list[AggregatedContradiction],
        sentence_cache: _SentenceCache | None = None,
    ) -> MultiSourceAggregationOutput:
        return MultiSourceAggregationOutput(
            session_id=input_data.session_id,
            correlation_id=input_data.correlation_id,
            status=self._resolve_output_status(input_data.items),
            source_type=self._resolve_source_type(extracted_items),
            total_items=len(input_data.items),
            extracted_items=len(extracted_items),
            used_source_count=0,
            overview=self._build_overview(extracted_items),
            key_claims=self._fallback_claims(
                extracted_items,
                source_weights,
                sentence_cache=sentence_cache,
            ),
            contradictions=contradiction_hints,
            complementary_points=self._build_complementary_points(extracted_items),
            duplicate_signals=duplicate_signals,
            source_weights=source_weights,
            source_coverage=[],
            relationship_signal=input_data.relationship_signal,
            entities=self._extract_entities_from_documents(extracted_items),
            topic_tags=self._extract_tags_from_documents(extracted_items),
            total_estimated_consumption_time_min=self._estimate_consumption_time_minutes(
                extracted_items
            ),
            metadata={"generation_mode": "heuristic_fallback"},
        )

    @staticmethod
    def _resolve_output_status(items: list[SourceExtractionItemResult]) -> str:
        has_extracted = any(item.status == AggregationItemStatus.EXTRACTED.value for item in items)
        has_failed = any(item.status == AggregationItemStatus.FAILED.value for item in items)
        if has_failed and has_extracted:
            return AggregationSessionStatus.PARTIAL.value
        if has_extracted:
            return AggregationSessionStatus.COMPLETED.value
        return AggregationSessionStatus.FAILED.value

    def _build_llm_context(
        self,
        *,
        input_data: MultiSourceAggregationInput,
        extracted_items: list[SourceExtractionItemResult],
        source_weights: list[AggregationSourceWeight],
        duplicate_signals: list[DuplicateSignal],
        contradiction_hints: list[AggregatedContradiction],
    ) -> str:
        source_context = []
        for item, weight in zip(extracted_items, source_weights, strict=True):
            document = item.normalized_document
            if document is None:
                continue
            source_context.append(
                {
                    "position": item.position,
                    "source_item_id": document.source_item_id,
                    "source_kind": document.source_kind.value,
                    "title": document.title,
                    "text": self._document_snippet(document),
                    "text_blocks": [
                        {
                            "kind": block.kind.value,
                            "text": _truncate(block.text, 280),
                            "confidence": block.confidence,
                        }
                        for block in document.text_blocks[:8]
                    ],
                    "media_count": len(document.media),
                    "metadata": _select_metadata(document.metadata),
                    "weight": weight.model_dump(mode="json"),
                }
            )

        payload = {
            "correlation_id": input_data.correlation_id,
            "language": input_data.language,
            "relationship_signal": input_data.relationship_signal.model_dump(mode="json")
            if input_data.relationship_signal
            else None,
            "duplicate_signals": [signal.model_dump(mode="json") for signal in duplicate_signals],
            "contradiction_hints": [
                contradiction.model_dump(mode="json") for contradiction in contradiction_hints
            ],
            "source_weighting_rules": {
                kind.value: weight for kind, weight in _EVIDENCE_BASE_WEIGHTS.items()
            },
            "sources": source_context,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _build_source_weight(self, item: SourceExtractionItemResult) -> AggregationSourceWeight:
        document = item.normalized_document
        if document is None:
            msg = "Expected normalized document for extracted item"
            raise ValueError(msg)

        evidence_weights: list[AggregationEvidenceWeight] = []
        if _has_text_evidence(document):
            evidence_weights.append(
                AggregationEvidenceWeight(
                    kind=AggregationEvidenceKind.TEXT,
                    weight=_EVIDENCE_BASE_WEIGHTS[AggregationEvidenceKind.TEXT],
                    rationale="Primary article or caption/body text is present.",
                )
            )
        if _has_transcript_evidence(document):
            evidence_weights.append(
                AggregationEvidenceWeight(
                    kind=AggregationEvidenceKind.TRANSCRIPT,
                    weight=_EVIDENCE_BASE_WEIGHTS[AggregationEvidenceKind.TRANSCRIPT],
                    rationale="Transcript content can support time-based media claims.",
                )
            )
        if _has_image_evidence(document):
            evidence_weights.append(
                AggregationEvidenceWeight(
                    kind=AggregationEvidenceKind.IMAGE,
                    weight=_EVIDENCE_BASE_WEIGHTS[AggregationEvidenceKind.IMAGE],
                    rationale="Media or alt-text adds non-textual context.",
                )
            )
        if _has_ocr_evidence(document):
            evidence_weights.append(
                AggregationEvidenceWeight(
                    kind=AggregationEvidenceKind.OCR,
                    weight=_EVIDENCE_BASE_WEIGHTS[AggregationEvidenceKind.OCR],
                    rationale="OCR-derived text is lower confidence than authored text.",
                )
            )
        if _has_metadata_evidence(document):
            evidence_weights.append(
                AggregationEvidenceWeight(
                    kind=AggregationEvidenceKind.METADATA,
                    weight=_EVIDENCE_BASE_WEIGHTS[AggregationEvidenceKind.METADATA],
                    rationale="Structured metadata can anchor titles, IDs, and authorship.",
                )
            )

        total_weight = round(sum(entry.weight for entry in evidence_weights), 2)
        return AggregationSourceWeight(
            source_item_id=document.source_item_id,
            source_kind=document.source_kind,
            total_weight=total_weight,
            evidence_weights=evidence_weights,
            rationale="Higher weight is assigned to authored text, then transcript/media, then OCR/metadata.",
        )

    def _build_source_coverage(
        self,
        *,
        items: list[SourceExtractionItemResult],
        output: MultiSourceAggregationOutput,
        weight_by_source_id: dict[str, AggregationSourceWeight],
    ) -> list[SourceCoverageEntry]:
        claim_ids_by_source: dict[str, list[str]] = defaultdict(list)
        contradiction_count_by_source: dict[str, int] = defaultdict(int)
        duplicate_count_by_source: dict[str, int] = defaultdict(int)

        for claim in output.key_claims:
            for source_item_id in claim.source_item_ids:
                claim_ids_by_source[source_item_id].append(claim.claim_id)

        for contradiction in output.contradictions:
            for source_item_id in contradiction.source_item_ids:
                contradiction_count_by_source[source_item_id] += 1

        for signal in output.duplicate_signals:
            for source_item_id in signal.source_item_ids:
                duplicate_count_by_source[source_item_id] += 1

        coverage: list[SourceCoverageEntry] = []
        for item in items:
            source_item_id = item.source_item_id
            claim_ids = (
                claim_ids_by_source.get(source_item_id, [])
                if item.status != AggregationItemStatus.DUPLICATE.value
                else []
            )
            contradiction_count = (
                contradiction_count_by_source.get(source_item_id, 0)
                if item.status != AggregationItemStatus.DUPLICATE.value
                else 0
            )
            duplicate_signal_count = duplicate_count_by_source.get(source_item_id, 0)
            used_in_summary = item.status != AggregationItemStatus.DUPLICATE.value and bool(
                claim_ids or contradiction_count or duplicate_signal_count
            )
            coverage.append(
                SourceCoverageEntry(
                    position=item.position,
                    item_id=item.item_id,
                    source_item_id=source_item_id,
                    source_kind=item.source_kind,
                    status=item.status,
                    used_in_summary=used_in_summary,
                    claim_ids=claim_ids,
                    contradiction_count=contradiction_count,
                    duplicate_signal_count=duplicate_signal_count,
                    total_weight=weight_by_source_id.get(source_item_id).total_weight
                    if source_item_id in weight_by_source_id
                    else None,
                )
            )
        return coverage

    def _parse_claims(
        self,
        raw_claims: Any,
        valid_source_ids: set[str],
    ) -> list[AggregatedClaim]:
        if not isinstance(raw_claims, list):
            return []

        claims: list[AggregatedClaim] = []
        for index, raw_claim in enumerate(raw_claims, 1):
            if not isinstance(raw_claim, dict):
                continue
            text = str(raw_claim.get("claim") or raw_claim.get("text") or "").strip()
            source_item_ids = _filter_source_item_ids(
                raw_claim.get("source_item_ids"),
                valid_source_ids,
            )
            if not text or not source_item_ids:
                continue
            evidence_kinds = _parse_evidence_kinds(raw_claim.get("evidence_kinds"))
            confidence = raw_claim.get("confidence")
            claims.append(
                AggregatedClaim(
                    claim_id=str(raw_claim.get("claim_id") or f"claim_{index}"),
                    text=text,
                    source_item_ids=source_item_ids,
                    evidence_kinds=evidence_kinds,
                    confidence=float(confidence) if isinstance(confidence, (int, float)) else None,
                )
            )
        return claims

    def _parse_contradictions(
        self,
        raw_contradictions: Any,
        valid_source_ids: set[str],
    ) -> list[AggregatedContradiction]:
        if not isinstance(raw_contradictions, list):
            return []

        contradictions: list[AggregatedContradiction] = []
        for raw_contradiction in raw_contradictions:
            if not isinstance(raw_contradiction, dict):
                continue
            source_item_ids = _filter_source_item_ids(
                raw_contradiction.get("source_item_ids"),
                valid_source_ids,
            )
            summary = str(
                raw_contradiction.get("summary") or raw_contradiction.get("text") or ""
            ).strip()
            if not summary or len(source_item_ids) < 2:
                continue
            contradictions.append(
                AggregatedContradiction(
                    summary=summary,
                    source_item_ids=source_item_ids,
                    resolution_note=str(raw_contradiction.get("resolution_note") or "").strip()
                    or None,
                )
            )
        return contradictions

    def _parse_duplicate_signals(
        self,
        raw_signals: Any,
        valid_source_ids: set[str],
    ) -> list[DuplicateSignal]:
        if not isinstance(raw_signals, list):
            return []

        signals: list[DuplicateSignal] = []
        for raw_signal in raw_signals:
            if not isinstance(raw_signal, dict):
                continue
            source_item_ids = _filter_source_item_ids(
                raw_signal.get("source_item_ids"),
                valid_source_ids,
            )
            summary = str(raw_signal.get("summary") or raw_signal.get("text") or "").strip()
            if not summary or len(source_item_ids) < 2:
                continue
            signals.append(DuplicateSignal(summary=summary, source_item_ids=source_item_ids))
        return signals

    def _fallback_claims(
        self,
        extracted_items: list[SourceExtractionItemResult],
        source_weights: list[AggregationSourceWeight],
        sentence_cache: _SentenceCache | None = None,
    ) -> list[AggregatedClaim]:
        sentence_cache = sentence_cache or _SentenceCache()
        weights_by_source = {weight.source_item_id: weight for weight in source_weights}
        sorted_items = sorted(
            extracted_items,
            key=lambda item: weights_by_source[item.source_item_id].total_weight,
            reverse=True,
        )
        claims: list[AggregatedClaim] = []
        for index, item in enumerate(sorted_items[:5], 1):
            document = item.normalized_document
            if document is None:
                continue
            snippet = self._best_claim_snippet(document, sentence_cache=sentence_cache)
            if not snippet:
                continue
            weight = weights_by_source[document.source_item_id]
            claims.append(
                AggregatedClaim(
                    claim_id=f"claim_{index}",
                    text=snippet,
                    source_item_ids=[document.source_item_id],
                    evidence_kinds=[entry.kind for entry in weight.evidence_weights],
                    confidence=min(1.0, round(weight.total_weight / 2.5, 2)),
                )
            )
        return claims

    def _build_overview(self, extracted_items: list[SourceExtractionItemResult]) -> str:
        kinds = sorted({item.source_kind.value for item in extracted_items})
        titles = [
            document.title
            for item in extracted_items
            if (document := item.normalized_document) is not None and document.title
        ]
        title_fragment = ", ".join(titles[:3]) if titles else "multiple source items"
        kind_fragment = ", ".join(kinds[:4])
        return (
            f"This bundle synthesizes {len(extracted_items)} extracted sources across {kind_fragment}. "
            f"Primary coverage comes from {title_fragment}."
        )

    def _build_complementary_points(
        self, extracted_items: list[SourceExtractionItemResult]
    ) -> list[str]:
        points: list[str] = []
        kinds = {item.source_kind for item in extracted_items}
        if len(kinds) > 1:
            points.append(
                "The bundle combines multiple source types, allowing text, media, and platform context to reinforce each other."
            )
        if any(_has_image_evidence(item.normalized_document) for item in extracted_items):
            points.append(
                "Visual evidence supplements the authored text, which helps preserve context that a text-only summary would drop."
            )
        if any(_has_transcript_evidence(item.normalized_document) for item in extracted_items):
            points.append(
                "Transcript evidence adds spoken context that can confirm or expand on captions and titles."
            )
        return points[:4]

    def _detect_duplicate_signals(
        self,
        extracted_items: list[SourceExtractionItemResult],
        *,
        sentence_cache: _SentenceCache | None = None,
    ) -> list[DuplicateSignal]:
        sentence_cache = sentence_cache or _SentenceCache()
        sentence_sources: dict[str, set[str]] = defaultdict(set)
        sentence_examples: dict[str, str] = {}
        for item in extracted_items:
            document = item.normalized_document
            if document is None:
                continue
            for sentence in self._document_sentences(document, sentence_cache=sentence_cache):
                canonical = _canonical_sentence(sentence)
                if len(canonical.split()) < 6:
                    continue
                sentence_sources[canonical].add(document.source_item_id)
                sentence_examples.setdefault(canonical, sentence.strip())

        duplicate_signals: list[DuplicateSignal] = []
        for canonical, source_ids in sentence_sources.items():
            if len(source_ids) < 2:
                continue
            duplicate_signals.append(
                DuplicateSignal(
                    summary=_truncate(sentence_examples[canonical], 160),
                    source_item_ids=sorted(source_ids),
                )
            )
        duplicate_signals.sort(key=lambda signal: (-len(signal.source_item_ids), signal.summary))
        return duplicate_signals[:5]

    def _detect_contradiction_hints(
        self,
        extracted_items: list[SourceExtractionItemResult],
        *,
        sentence_cache: _SentenceCache | None = None,
    ) -> list[AggregatedContradiction]:
        sentence_cache = sentence_cache or _SentenceCache()
        sentence_groups: dict[str, list[tuple[str, str, tuple[str, ...]]]] = defaultdict(list)
        for item in extracted_items:
            document = item.normalized_document
            if document is None:
                continue
            for sentence in self._document_sentences(document, sentence_cache=sentence_cache):
                numbers = tuple(sorted(_NUMBER_RE.findall(sentence)))
                if len(numbers) == 0:
                    continue
                base = _numeric_sentence_base(sentence)
                if len(base.split()) < 4:
                    continue
                sentence_groups[base].append((document.source_item_id, sentence.strip(), numbers))

        contradictions: list[AggregatedContradiction] = []
        for grouped_sentences in sentence_groups.values():
            distinct_numbers = {entry[2] for entry in grouped_sentences}
            if len(distinct_numbers) < 2:
                continue
            source_item_ids = sorted({entry[0] for entry in grouped_sentences})
            if len(source_item_ids) < 2:
                continue
            example_sentences = "; ".join(
                _truncate(entry[1], 120) for entry in grouped_sentences[:2]
            )
            contradictions.append(
                AggregatedContradiction(
                    summary=f"Potential numeric disagreement detected: {example_sentences}",
                    source_item_ids=source_item_ids,
                    resolution_note="Verify the conflicting figures against the highest-weight sources.",
                )
            )
        return contradictions[:4]

    def _resolve_source_type(self, extracted_items: Iterable[SourceExtractionItemResult]) -> str:
        kinds = sorted(
            {
                item.source_kind.value
                for item in extracted_items
                if item.status == AggregationItemStatus.EXTRACTED.value
            }
        )
        if len(kinds) == 1:
            return kinds[0]
        return "mixed"

    def _estimate_consumption_time_minutes(
        self, extracted_items: list[SourceExtractionItemResult]
    ) -> int | None:
        total_minutes = 0
        for item in extracted_items:
            document = item.normalized_document
            if document is None:
                continue
            metadata_minutes = _coerce_int(
                document.metadata.get("estimated_reading_time_min")
                or document.metadata.get("reading_time_min")
            )
            if metadata_minutes is not None:
                total_minutes += metadata_minutes
                continue
            duration_seconds = 0.0
            for asset in document.media:
                if asset.duration_sec:
                    duration_seconds = max(duration_seconds, float(asset.duration_sec))
            if duration_seconds > 0:
                total_minutes += max(1, round(duration_seconds / 60))
                continue
            word_count = len(document.text.split())
            if word_count > 0:
                total_minutes += max(1, round(word_count / 220))
        return total_minutes or None

    def _extract_entities_from_documents(
        self, extracted_items: list[SourceExtractionItemResult]
    ) -> list[str]:
        entities: list[str] = []
        for item in extracted_items:
            document = item.normalized_document
            if document is None:
                continue
            raw_entities = document.metadata.get("entities")
            if not isinstance(raw_entities, list):
                continue
            for entity in raw_entities:
                if isinstance(entity, dict) and "name" in entity:
                    entities.append(str(entity["name"]))
                elif isinstance(entity, str):
                    entities.append(entity)
        return _clean_string_list(entities)

    def _extract_tags_from_documents(
        self, extracted_items: list[SourceExtractionItemResult]
    ) -> list[str]:
        tags: list[str] = []
        for item in extracted_items:
            document = item.normalized_document
            if document is None:
                continue
            raw_tags = document.metadata.get("topic_tags")
            if isinstance(raw_tags, list):
                tags.extend(str(tag) for tag in raw_tags if str(tag).strip())
            tags.extend(
                f"#{match.group(1).lower()}" for match in _HASHTAG_RE.finditer(document.text)
            )
        return _normalize_tags(tags)

    def _document_sentences(
        self,
        document: NormalizedSourceDocument,
        *,
        sentence_cache: _SentenceCache | None = None,
    ) -> list[str]:
        cache = sentence_cache or _SentenceCache()
        return cache.document_sentences(document)

    def _document_snippet(self, document: NormalizedSourceDocument) -> str:
        if document.text.strip():
            return _truncate(document.text, 900)
        snippets = [block.text for block in document.text_blocks if block.text.strip()]
        return _truncate(" ".join(snippets), 900)

    def _best_claim_snippet(
        self,
        document: NormalizedSourceDocument,
        *,
        sentence_cache: _SentenceCache | None = None,
    ) -> str:
        cache = sentence_cache or _SentenceCache()
        preferred_kinds = (
            ExtractedTextKind.BODY,
            ExtractedTextKind.CAPTION,
            ExtractedTextKind.TRANSCRIPT,
            ExtractedTextKind.OCR,
            ExtractedTextKind.TITLE,
        )
        for preferred_kind in preferred_kinds:
            for block in document.text_blocks:
                if block.kind != preferred_kind:
                    continue
                sentence = next(
                    (
                        candidate
                        for candidate in cache.block_sentences(document, block)
                        if len(candidate.split()) >= 6
                    ),
                    None,
                )
                if sentence:
                    return _truncate(sentence, 220)
        if document.title:
            return _truncate(document.title, 220)
        return _truncate(document.text, 220)

    def _load_prompt(self, language: str) -> str:
        lang = language.lower() if language.lower() in ("en", "ru") else "en"
        prompt_file = _PROMPT_DIR / f"multi_source_aggregation_system_{lang}.txt"
        try:
            return read_prompt_text(prompt_file)
        except FileNotFoundError:
            return read_prompt_text(_PROMPT_DIR / "multi_source_aggregation_system_en.txt")


__all__ = [
    "MultiSourceAggregationAgent",
    "MultiSourceAggregationInput",
]
