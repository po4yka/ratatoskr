"""Agent for detecting relationships between batch articles."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from app.adapter_models.batch_analysis import (
    ArticleMetadata,
    ClusterInfo,
    RelationshipAnalysisInput,
    RelationshipAnalysisOutput,
    RelationshipType,
    SeriesInfo,
)
from app.agents.base_agent import AgentResult, BaseAgent
from app.agents.llm_call_persistence import persist_agent_llm_call
from app.core.content_cleaner import wrap_untrusted_source
from app.core.logging_utils import get_logger
from app.prompts.file_cache import read_prompt_text

if TYPE_CHECKING:
    from app.application.ports.llm_client import LLMClientProtocol
    from app.application.ports.requests import LLMRepositoryPort

logger = get_logger(__name__)

# Prompt directory
_PROMPT_DIR = Path(__file__).parent.parent / "prompts"


class _RelationshipLLMResponse(RelationshipAnalysisOutput):
    """Strict provider schema including every field consumed by the parser."""

    model_config = ConfigDict(extra="forbid")

    signals_used: list[str]


# Patterns for series detection
SERIES_PATTERNS = [
    re.compile(r"(?:part|часть)\s*(\d+)", re.IGNORECASE),
    re.compile(r"(?:chapter|глава)\s*(\d+)", re.IGNORECASE),
    re.compile(r"(?:episode|эпизод)\s*(\d+)", re.IGNORECASE),
    re.compile(r"(?:volume|том)\s*(\d+)", re.IGNORECASE),
    re.compile(r"\((\d+)/\d+\)"),  # (1/3) format
    re.compile(r"#(\d+)(?:\s|$)"),  # #1 format
    re.compile(r"(?:^|\s)(\d+)\.(?:\s|$)"),  # "1. Title" format
]


class MetadataSignals(BaseModel):
    """Signals extracted from metadata analysis."""

    model_config = ConfigDict(frozen=True)

    same_author: bool = False
    same_domain: bool = False
    series_numbers: list[tuple[int, int]] = Field(default_factory=list)  # (request_id, number)
    shared_entities: list[str] = Field(default_factory=list)
    shared_tags: list[str] = Field(default_factory=list)
    title_similarity: float = 0.0
    entity_overlap_ratio: float = 0.0
    tag_overlap_ratio: float = 0.0


class RelationshipAnalysisAgent(BaseAgent[RelationshipAnalysisInput, RelationshipAnalysisOutput]):
    """Agent for detecting relationships between articles in a batch.

    Uses a multi-signal approach:
    1. Metadata signals (fast): Same author, domain, explicit numbering in titles
    2. Entity/tag overlap: Shared entities, topic_tags from summary JSONs
    3. LLM analysis (fallback): Only for ambiguous cases

    The agent tries to avoid LLM calls when metadata signals are strong enough.
    """

    def __init__(
        self,
        llm_client: LLMClientProtocol | None = None,
        correlation_id: str | None = None,
        *,
        llm_repo: LLMRepositoryPort | None = None,
    ):
        super().__init__(name="RelationshipAnalysisAgent", correlation_id=correlation_id)
        self._llm = llm_client
        # DI supplies this so the ambiguous-case LLM call is persisted to
        # llm_calls against one of the analysed article requests (rule 3).
        self._llm_repo = llm_repo

    async def execute(
        self, input_data: RelationshipAnalysisInput
    ) -> AgentResult[RelationshipAnalysisOutput]:
        """Analyze relationships between articles."""
        self.correlation_id = input_data.correlation_id
        articles = input_data.articles

        if len(articles) < 2:
            return AgentResult.success_result(
                RelationshipAnalysisOutput(
                    relationship_type=RelationshipType.UNRELATED,
                    confidence=1.0,
                    reasoning="Single article - no relationship possible",
                    signals_used=["article_count"],
                )
            )

        self.log_info(f"Analyzing relationships for {len(articles)} articles")

        # Phase 1: Extract metadata signals (fast, no LLM)
        signals = self._extract_metadata_signals(articles)
        signals_used = []

        # Phase 2: Check for series (explicit numbering)
        if len(signals.series_numbers) == len(articles):
            series_result = self._detect_series(articles, signals)
            if series_result and series_result.confidence >= input_data.series_threshold:
                self.log_info(
                    f"Detected series with confidence {series_result.confidence:.2f}",
                    pattern=series_result.series_info.numbering_pattern
                    if series_result.series_info
                    else None,
                )
                return AgentResult.success_result(series_result)
            signals_used.append("series_patterns")

        # Phase 3: Check for strong topic cluster (metadata-based)
        cluster_result = self._detect_cluster_from_metadata(
            articles, signals, input_data.cluster_threshold
        )
        if cluster_result and cluster_result.confidence >= input_data.cluster_threshold:
            self.log_info(f"Detected topic cluster with confidence {cluster_result.confidence:.2f}")
            return AgentResult.success_result(cluster_result)

        # Track what signals we've checked
        if signals.same_author:
            signals_used.append("same_author")
        if signals.same_domain:
            signals_used.append("same_domain")
        if signals.shared_entities:
            signals_used.append("entity_overlap")
        if signals.shared_tags:
            signals_used.append("tag_overlap")

        # Phase 4: Use LLM for ambiguous cases (if available)
        if self._llm and self._should_use_llm(signals, input_data.cluster_threshold):
            try:
                llm_result = await self._analyze_with_llm(articles, input_data.language)
                if llm_result:
                    llm_result.signals_used.extend(signals_used)
                    llm_result.signals_used.append("llm_analysis")
                    return AgentResult.success_result(llm_result)
            except Exception as e:
                self.log_warning(f"LLM analysis failed: {e}")

        # Phase 5: Return domain/author-based relationship if applicable
        if signals.same_author:
            return AgentResult.success_result(
                RelationshipAnalysisOutput(
                    relationship_type=RelationshipType.AUTHOR_COLLECTION,
                    confidence=0.7,
                    cluster_info=ClusterInfo(
                        cluster_topic=None,
                        shared_entities=list(signals.shared_entities),
                        shared_tags=list(signals.shared_tags),
                        perspectives=[],
                        confidence=0.7,
                    ),
                    reasoning="Same author across all articles",
                    signals_used=["same_author", *signals_used],
                )
            )

        if signals.same_domain and (signals.shared_entities or signals.shared_tags):
            return AgentResult.success_result(
                RelationshipAnalysisOutput(
                    relationship_type=RelationshipType.DOMAIN_RELATED,
                    confidence=0.5,
                    cluster_info=ClusterInfo(
                        cluster_topic=None,
                        shared_entities=list(signals.shared_entities),
                        shared_tags=list(signals.shared_tags),
                        perspectives=[],
                        confidence=0.5,
                    ),
                    reasoning="Same domain with some shared entities/tags",
                    signals_used=["same_domain", *signals_used],
                )
            )

        # Default: unrelated
        return AgentResult.success_result(
            RelationshipAnalysisOutput(
                relationship_type=RelationshipType.UNRELATED,
                confidence=0.8,
                reasoning="No strong relationship signals detected",
                signals_used=signals_used or ["no_signals"],
            )
        )

    def _extract_metadata_signals(self, articles: list[ArticleMetadata]) -> MetadataSignals:
        """Extract relationship signals from article metadata."""
        # Check author
        authors = [a.author for a in articles if a.author]
        same_author = len(set(authors)) == 1 and len(authors) == len(articles)

        # Check domain
        domains = []
        for a in articles:
            if a.domain:
                domains.append(a.domain)
            elif a.url:
                try:
                    domains.append(urlparse(a.url).netloc)
                except Exception:
                    logger.debug(
                        "relationship_domain_parse_failed",
                        extra={"url": a.url},
                        exc_info=True,
                    )
        same_domain = len(set(domains)) == 1 and len(domains) >= 2

        # Detect series numbering in titles
        series_numbers = []
        for article in articles:
            if not article.title:
                continue
            for pattern in SERIES_PATTERNS:
                match = pattern.search(article.title)
                if match:
                    try:
                        num = int(match.group(1))
                        series_numbers.append((article.request_id, num))
                        break
                    except (ValueError, IndexError):
                        logger.debug(
                            "relationship_series_number_parse_failed",
                            extra={"title": article.title},
                            exc_info=True,
                        )

        # Find shared entities
        all_entities: list[set[str]] = []
        for a in articles:
            entities = {e.lower().strip() for e in a.entities if e}
            all_entities.append(entities)

        shared_entities: list[str] = []
        if len(all_entities) >= 2:
            shared = all_entities[0]
            for es in all_entities[1:]:
                shared = shared & es
            shared_entities = sorted(shared)

        # Find shared tags
        all_tags: list[set[str]] = []
        for a in articles:
            tags = {t.lower().strip().lstrip("#") for t in a.topic_tags if t}
            all_tags.append(tags)

        shared_tags: list[str] = []
        if len(all_tags) >= 2:
            shared = all_tags[0]
            for ts in all_tags[1:]:
                shared = shared & ts
            shared_tags = sorted(shared)

        # Calculate overlap ratios
        entity_overlap_ratio = 0.0
        if all_entities:
            total_unique = len(set().union(*all_entities))
            if total_unique > 0:
                entity_overlap_ratio = len(shared_entities) / total_unique

        tag_overlap_ratio = 0.0
        if all_tags:
            total_unique = len(set().union(*all_tags))
            if total_unique > 0:
                tag_overlap_ratio = len(shared_tags) / total_unique

        return MetadataSignals(
            same_author=same_author,
            same_domain=same_domain,
            series_numbers=series_numbers,
            shared_entities=shared_entities,
            shared_tags=shared_tags,
            entity_overlap_ratio=entity_overlap_ratio,
            tag_overlap_ratio=tag_overlap_ratio,
        )

    def _detect_series(
        self, articles: list[ArticleMetadata], signals: MetadataSignals
    ) -> RelationshipAnalysisOutput | None:
        """Detect if articles form a series."""
        if len(signals.series_numbers) < 2 or len(signals.series_numbers) != len(articles):
            return None

        # Sort by detected number
        sorted_nums = sorted(signals.series_numbers, key=lambda x: x[1])
        request_ids = [r[0] for r in sorted_nums]
        numbers = [r[1] for r in sorted_nums]

        # Check if numbers are sequential or close
        is_sequential = all(numbers[i] + 1 == numbers[i + 1] for i in range(len(numbers) - 1))

        # Detect the numbering pattern
        pattern = None
        sample_title = next((a.title for a in articles if a.request_id == request_ids[0]), "")
        if sample_title:
            for p in SERIES_PATTERNS:
                if p.search(sample_title):
                    # Extract pattern type
                    pattern_text = p.pattern
                    if "part" in pattern_text.lower():
                        pattern = "Part N"
                    elif "chapter" in pattern_text.lower():
                        pattern = "Chapter N"
                    elif "часть" in pattern_text.lower():
                        pattern = "Часть N"
                    elif "глава" in pattern_text.lower():
                        pattern = "Глава N"
                    else:
                        pattern = "Numeric"
                    break

        # Calculate confidence
        confidence = 0.7
        if is_sequential:
            confidence = 0.95
        elif len(signals.series_numbers) == len(articles):
            confidence = 0.85

        if signals.same_author:
            confidence = min(1.0, confidence + 0.05)
        if signals.same_domain:
            confidence = min(1.0, confidence + 0.05)

        # Try to extract series title (common prefix)
        titles = [a.title for a in articles if a.title]
        series_title = self._extract_common_prefix(titles) if titles else None

        return RelationshipAnalysisOutput(
            relationship_type=RelationshipType.SERIES,
            confidence=confidence,
            series_info=SeriesInfo(
                series_title=series_title,
                article_order=request_ids,
                numbering_pattern=pattern,
                confidence=confidence,
            ),
            reasoning=f"Detected sequential numbering pattern ({pattern or 'numeric'}) in titles",
            signals_used=["series_patterns", "title_numbering"],
        )

    def _extract_common_prefix(self, titles: list[str]) -> str | None:
        """Extract common prefix from titles (potential series name)."""
        if not titles or len(titles) < 2:
            return None

        # Remove numbering patterns to find common base
        cleaned = []
        for title in titles:
            clean = title
            for pattern in SERIES_PATTERNS:
                clean = pattern.sub("", clean)
            clean = clean.strip(" -:.")
            if clean:
                cleaned.append(clean)

        if not cleaned:
            return None

        # Find common prefix
        prefix = os.path.commonprefix(cleaned).strip(" -:.")
        return prefix if len(prefix) >= 10 else None

    def _detect_cluster_from_metadata(
        self,
        articles: list[ArticleMetadata],
        signals: MetadataSignals,
        threshold: float,
    ) -> RelationshipAnalysisOutput | None:
        """Detect topic cluster from metadata signals alone."""
        # Need either strong entity overlap or tag overlap
        strong_overlap = (
            len(signals.shared_entities) >= 3
            or len(signals.shared_tags) >= 3
            or (signals.entity_overlap_ratio >= 0.3 and len(signals.shared_entities) >= 2)
            or (signals.tag_overlap_ratio >= 0.3 and len(signals.shared_tags) >= 2)
        )

        if not strong_overlap:
            return None

        # Calculate confidence based on overlap strength
        confidence = 0.5
        if len(signals.shared_entities) >= 3:
            confidence += 0.15
        if len(signals.shared_tags) >= 3:
            confidence += 0.15
        if signals.entity_overlap_ratio >= 0.4:
            confidence += 0.1
        if signals.tag_overlap_ratio >= 0.4:
            confidence += 0.1
        if signals.same_author:
            confidence += 0.05
        if signals.same_domain:
            confidence += 0.05

        confidence = min(0.95, confidence)

        if confidence < threshold:
            return None

        # Infer cluster topic from shared tags/entities
        cluster_topic = None
        if signals.shared_tags:
            cluster_topic = ", ".join(signals.shared_tags[:3])
        elif signals.shared_entities:
            cluster_topic = ", ".join(signals.shared_entities[:3])

        return RelationshipAnalysisOutput(
            relationship_type=RelationshipType.TOPIC_CLUSTER,
            confidence=confidence,
            cluster_info=ClusterInfo(
                cluster_topic=cluster_topic,
                shared_entities=list(signals.shared_entities),
                shared_tags=list(signals.shared_tags),
                perspectives=[],
                confidence=confidence,
            ),
            reasoning=f"Strong overlap in entities ({len(signals.shared_entities)}) and/or tags ({len(signals.shared_tags)})",
            signals_used=["entity_overlap", "tag_overlap"],
        )

    def _should_use_llm(self, signals: MetadataSignals, threshold: float) -> bool:
        """Determine if LLM analysis would be helpful."""
        # Use LLM if we have some signals but not enough for high confidence
        has_some_overlap = (
            len(signals.shared_entities) >= 1
            or len(signals.shared_tags) >= 1
            or signals.same_domain
        )
        not_strong_enough = (
            len(signals.shared_entities) < 3
            and len(signals.shared_tags) < 3
            and not signals.series_numbers
        )
        return has_some_overlap and not_strong_enough

    async def _analyze_with_llm(
        self, articles: list[ArticleMetadata], language: str
    ) -> RelationshipAnalysisOutput | None:
        """Use LLM for relationship analysis when metadata signals are ambiguous."""
        if not self._llm:
            return None

        prompt = self._load_prompt(language)

        # Build article descriptions for LLM
        article_descriptions = []
        for i, article in enumerate(articles, 1):
            desc = f"Article {i} (request_id: {article.request_id}):\n"
            desc += f"  Title: {article.title or 'N/A'}\n"
            desc += f"  URL: {article.url}\n"
            desc += f"  Author: {article.author or 'N/A'}\n"
            desc += f"  Domain: {article.domain or 'N/A'}\n"
            if article.topic_tags:
                desc += f"  Tags: {', '.join(article.topic_tags[:10])}\n"
            if article.entities:
                desc += f"  Entities: {', '.join(article.entities[:10])}\n"
            if article.summary_250:
                desc += f"  Summary: {article.summary_250}\n"
            article_descriptions.append(desc)

        user_content = (
            "Analyze the relationship between the article metadata inside the "
            "untrusted-source boundary.\n\n"
            + wrap_untrusted_source("\n".join(article_descriptions))
        )

        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_content},
        ]

        model = getattr(self._llm, "_model", "unknown")
        request_id = articles[0].request_id if articles else None
        t0 = time.monotonic()
        try:
            result = await self._llm.chat_structured(
                messages,
                response_model=_RelationshipLLMResponse,
                max_retries=3,
                max_tokens=1000,
                temperature=0.1,
                request_id=None,
            )
        except Exception as exc:
            # Local error handling (previously absent): log, persist the error
            # row, and degrade to None so execute()'s metadata fallback runs.
            self.log_warning(f"LLM relationship analysis failed: {exc}")
            await self._persist_llm_call(
                request_id=request_id,
                status="error",
                model=model,
                result=None,
                latency_ms=int((time.monotonic() - t0) * 1000),
                error=exc,
            )
            return None

        await self._persist_llm_call(
            request_id=request_id,
            status="success",
            model=model,
            result=result,
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
        return self._parse_llm_response(result.parsed.model_dump(), articles)

    async def _persist_llm_call(
        self,
        *,
        request_id: int | None,
        status: str,
        model: str,
        result: Any,
        latency_ms: int,
        error: Exception | None = None,
    ) -> None:
        """Best-effort persist of the analysis LLM call to ``llm_calls``.

        ``endpoint="relationship_analysis"`` keeps it queryable. The article
        request anchor is optional; persistence failures are logged and never
        propagated.
        """
        await persist_agent_llm_call(
            self._llm_repo,
            request_id=request_id,
            endpoint="relationship_analysis",
            model=model,
            status=status,
            result=result,
            latency_ms=latency_ms,
            error=error,
            correlation_id=self.correlation_id,
            structured_output_used=True,
            provider=getattr(self._llm, "provider_name", None),
        )

    def _parse_llm_response(
        self, parsed: dict[str, Any], articles: list[ArticleMetadata]
    ) -> RelationshipAnalysisOutput | None:
        """Parse LLM response into RelationshipAnalysisOutput."""
        try:
            rel_type_str = parsed.get("relationship_type", "unrelated").lower()
            rel_type = RelationshipType(rel_type_str)

            confidence = float(parsed.get("confidence", 0.5))
            confidence = max(0.0, min(1.0, confidence))

            series_info = None
            if rel_type == RelationshipType.SERIES and "series_info" in parsed:
                si = parsed["series_info"]
                series_info = SeriesInfo(
                    series_title=si.get("series_title"),
                    article_order=si.get("article_order", [a.request_id for a in articles]),
                    numbering_pattern=si.get("numbering_pattern"),
                    confidence=float(si.get("confidence", confidence)),
                )

            cluster_info = None
            if rel_type in (RelationshipType.TOPIC_CLUSTER, RelationshipType.AUTHOR_COLLECTION):
                ci = parsed.get("cluster_info", {})
                cluster_info = ClusterInfo(
                    cluster_topic=ci.get("cluster_topic"),
                    shared_entities=ci.get("shared_entities", []),
                    shared_tags=ci.get("shared_tags", []),
                    perspectives=ci.get("perspectives", []),
                    confidence=float(ci.get("confidence", confidence)),
                )

            return RelationshipAnalysisOutput(
                relationship_type=rel_type,
                confidence=confidence,
                series_info=series_info,
                cluster_info=cluster_info,
                reasoning=parsed.get("reasoning", ""),
                signals_used=parsed.get("signals_used", []),
            )
        except Exception as e:
            self.log_warning(f"Failed to parse LLM response: {e}")
            return None

    def _load_prompt(self, language: str) -> str:
        """Load the relationship analysis prompt."""
        lang = language.lower() if language.lower() in ("en", "ru") else "en"
        prompt_file = _PROMPT_DIR / f"relationship_analysis_system_{lang}.txt"

        try:
            return read_prompt_text(prompt_file)
        except FileNotFoundError:
            return read_prompt_text(_PROMPT_DIR / "relationship_analysis_system_en.txt")
