"""Web search agent for enriching article summarization with current context."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from app.adapters.content.search_context_builder import SearchContextBuilder
from app.agents.base_agent import AgentResult, BaseAgent, _tracer
from app.core.logging_utils import get_logger
from app.observability.attributes import AGENT_ATTEMPT, AGENT_NAME, REQUEST_CORRELATION_ID
from app.observability.metrics import record_llm_call_attempt, record_llm_call_latency
from app.prompts.file_cache import read_prompt_text

if TYPE_CHECKING:
    from app.adapters.llm import LLMClientProtocol
    from app.application.services.topic_search import TopicArticle, TopicSearchService
    from app.config import WebSearchConfig

logger = get_logger(__name__)

# Prompt directory
_PROMPT_DIR = Path(__file__).parent.parent / "prompts"


class SearchAnalysisResult(BaseModel):
    """Result of LLM search analysis."""

    model_config = ConfigDict(frozen=True)

    needs_search: bool
    queries: list[str]
    reason: str


class WebSearchAgentInput(BaseModel):
    """Input for the WebSearchAgent."""

    model_config = ConfigDict(frozen=True)

    content: str
    language: str = "en"
    correlation_id: str | None = None


class WebSearchAgentOutput(BaseModel):
    """Output from the WebSearchAgent."""

    model_config = ConfigDict(frozen=True)

    searched: bool
    context: str
    queries_executed: list[str]
    articles_found: int
    reason: str


class WebSearchAgent(BaseAgent[WebSearchAgentInput, WebSearchAgentOutput]):
    """Agent that determines if web search would improve summarization and executes searches.

    This agent implements a two-phase approach:
    1. Analyze content to identify knowledge gaps using LLM
    2. Execute targeted searches via Firecrawl if beneficial

    The agent uses the existing TopicSearchService for web search functionality.
    """

    def __init__(
        self,
        llm_client: LLMClientProtocol,
        search_service: TopicSearchService,
        cfg: WebSearchConfig,
        correlation_id: str | None = None,
    ):
        super().__init__(name="WebSearchAgent", correlation_id=correlation_id)
        self._llm = llm_client
        self._search = search_service
        self._cfg = cfg
        self._context_builder = SearchContextBuilder(max_chars=cfg.max_context_chars)

    async def execute(self, input_data: WebSearchAgentInput) -> AgentResult[WebSearchAgentOutput]:
        """Execute web search enrichment workflow.

        Args:
            input_data: Content and language information

        Returns:
            AgentResult with search context or empty result if search not needed
        """
        cid = input_data.correlation_id or self.correlation_id

        with _tracer.start_as_current_span("agent.web_search") as span:
            span.set_attribute(AGENT_NAME, "web_search")
            span.set_attribute(REQUEST_CORRELATION_ID, cid)
            span.set_attribute(AGENT_ATTEMPT, 1)

            # Skip if content too short
            if len(input_data.content) < self._cfg.min_content_length:
                self.log_info(
                    "content_too_short_for_search",
                    content_len=len(input_data.content),
                    min_required=self._cfg.min_content_length,
                )
                return AgentResult.success_result(
                    WebSearchAgentOutput(
                        searched=False,
                        context="",
                        queries_executed=[],
                        articles_found=0,
                        reason=f"Content too short ({len(input_data.content)} chars)",
                    )
                )

            # Phase 1: Analyze content for knowledge gaps
            try:
                analysis = await self._analyze_content(input_data.content, input_data.language, cid)
            except Exception as e:
                self.log_error("search_analysis_failed", error=str(e))
                return AgentResult.success_result(
                    WebSearchAgentOutput(
                        searched=False,
                        context="",
                        queries_executed=[],
                        articles_found=0,
                        reason=f"Analysis failed: {e}",
                    )
                )

            # Skip if no search needed
            if not analysis.needs_search or not analysis.queries:
                self.log_info("search_not_needed", reason=analysis.reason)
                return AgentResult.success_result(
                    WebSearchAgentOutput(
                        searched=False,
                        context="",
                        queries_executed=[],
                        articles_found=0,
                        reason=analysis.reason,
                    )
                )

            # Phase 2: Execute searches
            queries_to_run = analysis.queries[: self._cfg.max_queries]
            all_articles: list[TopicArticle] = []

            for query in queries_to_run:
                try:
                    articles = await self._search.find_articles(query, correlation_id=cid)
                    all_articles.extend(articles)
                    self.log_info(
                        "search_query_completed",
                        query=query,
                        results=len(articles),
                    )
                except Exception as e:
                    self.log_warning("search_query_failed", query=query, error=str(e))
                    continue

            # Build context from results
            context = self._context_builder.build_context(all_articles)

            self.log_info(
                "web_search_completed",
                queries_executed=len(queries_to_run),
                articles_found=len(all_articles),
                context_chars=len(context),
            )

            return AgentResult.success_result(
                WebSearchAgentOutput(
                    searched=True,
                    context=context,
                    queries_executed=queries_to_run,
                    articles_found=len(all_articles),
                    reason=analysis.reason,
                ),
                queries=queries_to_run,
                articles=len(all_articles),
            )

    async def _analyze_content(
        self, content: str, language: str, correlation_id: str | None
    ) -> SearchAnalysisResult:
        """Use LLM to analyze content and determine if search is needed.

        Args:
            content: Article content to analyze
            language: Target language
            correlation_id: Optional correlation ID

        Returns:
            SearchAnalysisResult with queries if search is recommended
        """
        # Load appropriate prompt
        prompt = self._load_analysis_prompt(language)

        # Truncate content for analysis (save tokens)
        content_preview = content[:8000] if len(content) > 8000 else content

        messages = [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": f"Analyze this content and determine if web search would help:\n\n{content_preview}",
            },
        ]

        # Make LLM call with latency tracking for Prometheus
        model = getattr(self._llm, "_model", "unknown")
        t0 = time.monotonic()
        try:
            result = await self._llm.chat_structured(
                messages,
                response_model=SearchAnalysisResult,
                max_retries=3,
                max_tokens=500,
                temperature=0.1,  # Low temperature for deterministic analysis
                request_id=None,  # No DB persistence for analysis
            )
            record_llm_call_attempt(provider="openrouter", model=model, status="success")
            record_llm_call_latency(model=model, latency_seconds=time.monotonic() - t0)
        except Exception:
            record_llm_call_attempt(provider="openrouter", model=model, status="error")
            record_llm_call_latency(model=model, latency_seconds=time.monotonic() - t0)
            raise

        return result.parsed

    def _load_analysis_prompt(self, language: str) -> str:
        """Load the search analysis prompt for the given language.

        Args:
            language: Language code ('en' or 'ru')

        Returns:
            Prompt text with current date injected
        """
        lang = language.lower() if language.lower() in ("en", "ru") else "en"
        prompt_file = _PROMPT_DIR / f"search_analysis_{lang}.txt"

        try:
            prompt = read_prompt_text(prompt_file)
        except FileNotFoundError:
            # Fall back to English
            prompt = read_prompt_text(_PROMPT_DIR / "search_analysis_en.txt")

        # Inject current date
        current_date = datetime.now(UTC).strftime("%Y-%m-%d")
        return prompt.replace("{current_date}", current_date)
