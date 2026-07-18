"""Agent for generating combined summaries across related articles."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ConfigDict

from app.adapter_models.batch_analysis import (
    CombinedSummaryInput,
    CombinedSummaryOutput,
    RelationshipType,
)
from app.agents.base_agent import AgentResult, BaseAgent, _tracer
from app.agents.llm_call_persistence import persist_agent_llm_call
from app.core.content_cleaner import wrap_untrusted_source
from app.core.logging_utils import get_logger
from app.observability.attributes import AGENT_ATTEMPT, AGENT_NAME, REQUEST_CORRELATION_ID
from app.prompts.file_cache import read_prompt_text

if TYPE_CHECKING:
    from app.adapters.llm import LLMClientProtocol
    from app.application.ports.requests import LLMRepositoryPort

logger = get_logger(__name__)

# Prompt directory
_PROMPT_DIR = Path(__file__).parent.parent / "prompts"


class _CombinedSummaryLLMResponse(CombinedSummaryOutput):
    """Strict provider schema mirroring the application output contract."""

    model_config = ConfigDict(extra="forbid")


class CombinedSummaryAgent(BaseAgent[CombinedSummaryInput, CombinedSummaryOutput]):
    """Agent for generating synthesized summaries when relationships are detected.

    This agent:
    - Synthesizes insights across related articles (not just concatenates)
    - Identifies thematic arcs and overarching narratives
    - Notes contradictions and different perspectives
    - Recommends optimal reading order with rationale
    - Combines entities and tags across the collection
    """

    def __init__(
        self,
        llm_client: LLMClientProtocol,
        correlation_id: str | None = None,
        *,
        llm_repo: LLMRepositoryPort | None = None,
        request_id: int | None = None,
    ):
        super().__init__(name="CombinedSummaryAgent", correlation_id=correlation_id)
        self._llm = llm_client
        # DI supplies these so the synthesis LLM call is persisted to llm_calls
        # against one of the batch's article requests (rule 3: persist everything).
        self._llm_repo = llm_repo
        self._request_id = request_id

    async def execute(self, input_data: CombinedSummaryInput) -> AgentResult[CombinedSummaryOutput]:
        """Generate combined summary for related articles."""
        self.correlation_id = input_data.correlation_id
        articles = input_data.articles
        relationship = input_data.relationship

        with _tracer.start_as_current_span("agent.combined_summary") as span:
            span.set_attribute(AGENT_NAME, "combined_summary")
            span.set_attribute(REQUEST_CORRELATION_ID, self.correlation_id)
            span.set_attribute(AGENT_ATTEMPT, 1)

            if len(articles) < 2:
                return AgentResult.error_result(
                    "Need at least 2 articles for combined summary",
                    article_count=len(articles),
                )

            if relationship.relationship_type == RelationshipType.UNRELATED:
                return AgentResult.error_result(
                    "Cannot generate combined summary for unrelated articles",
                    relationship_type=relationship.relationship_type.value,
                )

            self.log_info(
                f"Generating combined summary for {len(articles)} articles "
                f"(relationship: {relationship.relationship_type.value})"
            )

            try:
                result = await self._generate_combined_summary(input_data)
                if result:
                    self.log_info(
                        "Combined summary generated successfully",
                        insight_count=len(result.synthesized_insights),
                        reading_order_count=len(result.recommended_reading_order),
                    )
                    return AgentResult.success_result(result)
                return AgentResult.error_result("Failed to generate combined summary")
            except Exception as e:
                self.log_error(f"Combined summary generation failed: {e}")
                return AgentResult.error_result(f"Generation failed: {e}")

    async def _generate_combined_summary(
        self, input_data: CombinedSummaryInput
    ) -> CombinedSummaryOutput | None:
        """Use LLM to generate the combined summary."""
        prompt = self._load_prompt(input_data.language)

        # Build context for LLM
        context = self._build_llm_context(input_data)

        messages = [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": (
                    "Synthesize the article data inside the untrusted-source boundary.\n\n"
                    + wrap_untrusted_source(context)
                ),
            },
        ]

        model = getattr(self._llm, "_model", "unknown")
        t0 = time.monotonic()
        try:
            result = await self._llm.chat_structured(
                messages,
                response_model=_CombinedSummaryLLMResponse,
                max_retries=3,
                max_tokens=2000,
                temperature=0.3,  # Slightly higher for creative synthesis
                request_id=None,
            )
        except Exception as exc:
            self.log_warning(f"LLM combined summary failed: {exc}")
            await self._persist_llm_call(
                status="error",
                model=model,
                result=None,
                latency_ms=int((time.monotonic() - t0) * 1000),
                error=exc,
            )
            return None

        await self._persist_llm_call(
            status="success",
            model=model,
            result=result,
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
        return self._parse_llm_response(result.parsed.model_dump(), input_data)

    async def _persist_llm_call(
        self,
        *,
        status: str,
        model: str,
        result: Any,
        latency_ms: int,
        error: Exception | None = None,
    ) -> None:
        """Best-effort persist of the synthesis LLM call to ``llm_calls``.

        ``endpoint="combined_summary"`` keeps it queryable/separable. The request
        anchor is optional so all agent calls remain auditable. Persistence failures
        are logged, never propagated.
        """
        await persist_agent_llm_call(
            self._llm_repo,
            request_id=self._request_id,
            endpoint="combined_summary",
            model=model,
            status=status,
            result=result,
            latency_ms=latency_ms,
            error=error,
            correlation_id=self.correlation_id,
            structured_output_used=True,
            provider=getattr(self._llm, "provider_name", None),
        )

    def _build_llm_context(self, input_data: CombinedSummaryInput) -> str:
        """Build the context string for the LLM."""
        parts = []

        # Relationship information
        rel = input_data.relationship
        parts.append(f"Relationship Type: {rel.relationship_type.value}")
        parts.append(f"Confidence: {rel.confidence:.2f}")
        if rel.reasoning:
            parts.append(f"Relationship Reasoning: {rel.reasoning}")
        parts.append("")

        # Series info if applicable
        if rel.series_info:
            parts.append("Series Information:")
            if rel.series_info.series_title:
                parts.append(f"  Series Title: {rel.series_info.series_title}")
            if rel.series_info.numbering_pattern:
                parts.append(f"  Numbering Pattern: {rel.series_info.numbering_pattern}")
            parts.append(f"  Article Order: {rel.series_info.article_order}")
            parts.append("")

        # Cluster info if applicable
        if rel.cluster_info:
            parts.append("Cluster Information:")
            if rel.cluster_info.cluster_topic:
                parts.append(f"  Topic: {rel.cluster_info.cluster_topic}")
            if rel.cluster_info.shared_entities:
                parts.append(
                    f"  Shared Entities: {', '.join(rel.cluster_info.shared_entities[:10])}"
                )
            if rel.cluster_info.shared_tags:
                parts.append(f"  Shared Tags: {', '.join(rel.cluster_info.shared_tags[:10])}")
            parts.append("")

        # Individual article summaries
        parts.append("Individual Article Summaries:")
        parts.append("=" * 50)

        for i, (article, full_summary) in enumerate(
            zip(input_data.articles, input_data.full_summaries, strict=True), 1
        ):
            parts.append(f"\nArticle {i} (request_id: {article.request_id}):")
            parts.append(f"Title: {article.title or 'N/A'}")
            parts.append(f"URL: {article.url}")
            if article.author:
                parts.append(f"Author: {article.author}")

            # Extract key fields from full summary
            if isinstance(full_summary, dict):
                if "summary_1000" in full_summary:
                    parts.append(f"Summary: {full_summary['summary_1000']}")
                elif "summary_250" in full_summary:
                    parts.append(f"Summary: {full_summary['summary_250']}")

                if ideas := full_summary.get("key_ideas"):
                    if isinstance(ideas, list):
                        parts.append("Key Ideas:")
                        for idea in ideas[:5]:
                            parts.append(f"  - {idea}")

                if tags := full_summary.get("topic_tags"):
                    if isinstance(tags, list):
                        parts.append(f"Tags: {', '.join(tags[:10])}")

                if entities := full_summary.get("entities"):
                    entities = full_summary["entities"]
                    if isinstance(entities, list):
                        entity_names = []
                        for e in entities[:10]:
                            if isinstance(e, dict) and "name" in e:
                                entity_names.append(e["name"])
                            elif isinstance(e, str):
                                entity_names.append(e)
                        if entity_names:
                            parts.append(f"Entities: {', '.join(entity_names)}")

                if "estimated_reading_time_min" in full_summary:
                    parts.append(f"Reading Time: {full_summary['estimated_reading_time_min']} min")

            parts.append("-" * 30)

        return "\n".join(parts)

    def _parse_llm_response(
        self, parsed: dict[str, Any], input_data: CombinedSummaryInput
    ) -> CombinedSummaryOutput | None:
        """Parse LLM response into CombinedSummaryOutput."""
        try:
            # Get recommended reading order
            reading_order = parsed.get("recommended_reading_order", [])
            if not reading_order:
                # Default to input order
                reading_order = [a.request_id for a in input_data.articles]

            # Ensure reading order contains valid request IDs
            valid_ids = {a.request_id for a in input_data.articles}
            reading_order = [rid for rid in reading_order if rid in valid_ids]
            if not reading_order:
                reading_order = [a.request_id for a in input_data.articles]

            # Calculate total reading time
            total_time = parsed.get("total_reading_time_min")
            if total_time is None:
                total_time = sum(
                    s.get("estimated_reading_time_min", 5)
                    for s in input_data.full_summaries
                    if isinstance(s, dict)
                )

            return CombinedSummaryOutput(
                thematic_arc=parsed.get("thematic_arc", ""),
                synthesized_insights=self._ensure_list(parsed.get("synthesized_insights", [])),
                contradictions=self._ensure_list(parsed.get("contradictions", [])),
                complementary_points=self._ensure_list(parsed.get("complementary_points", [])),
                recommended_reading_order=reading_order,
                reading_order_rationale=parsed.get("reading_order_rationale"),
                combined_key_ideas=self._ensure_list(parsed.get("combined_key_ideas", [])),
                combined_entities=self._ensure_list(parsed.get("combined_entities", [])),
                combined_topic_tags=self._ensure_list(parsed.get("combined_topic_tags", [])),
                total_reading_time_min=int(total_time) if total_time else None,
            )
        except Exception as e:
            self.log_warning(f"Failed to parse combined summary response: {e}")
            return None

    def _ensure_list(self, value: Any) -> list[str]:
        """Ensure value is a list of strings."""
        if not isinstance(value, list):
            return []
        return [str(item) for item in value if item]

    def _load_prompt(self, language: str) -> str:
        """Load the combined summary prompt."""
        lang = language.lower() if language.lower() in ("en", "ru") else "en"
        prompt_file = _PROMPT_DIR / f"combined_summary_system_{lang}.txt"

        try:
            return read_prompt_text(prompt_file)
        except FileNotFoundError:
            return read_prompt_text(_PROMPT_DIR / "combined_summary_system_en.txt")
