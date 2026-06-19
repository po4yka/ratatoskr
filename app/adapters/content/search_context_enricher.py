"""Optional web-search enrichment for summary prompts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.core.async_utils import raise_if_cancelled
from app.core.logging_utils import get_logger
from app.observability.metrics import record_web_search_decision

if TYPE_CHECKING:
    from app.application.services.topic_search import TopicSearchService

logger = get_logger(__name__)


class SearchContextEnricher:
    """Attach search-derived context when the feature is enabled."""

    def __init__(
        self,
        *,
        cfg: Any,
        openrouter: Any,
        topic_search: TopicSearchService | None = None,
    ) -> None:
        self._cfg = cfg
        self._openrouter = openrouter
        self._topic_search = topic_search

    async def enrich(
        self,
        *,
        content_text: str,
        chosen_lang: str,
        correlation_id: str | None,
    ) -> str:
        """Return formatted search context or an empty string."""
        if not self._cfg.web_search.enabled:
            record_web_search_decision("skipped_disabled")
            return ""

        if self._topic_search is None:
            record_web_search_decision("skipped_low_value")
            logger.debug(
                "web_search_skipped_no_service",
                extra={"cid": correlation_id},
            )
            return ""

        if len(content_text) < self._cfg.web_search.min_content_length:
            record_web_search_decision("skipped_low_value")
            logger.debug(
                "web_search_skipped_short_content",
                extra={
                    "cid": correlation_id,
                    "content_len": len(content_text),
                    "min_required": self._cfg.web_search.min_content_length,
                },
            )
            return ""

        try:  # nosemgrep: broad-except-base — except clause calls raise_if_cancelled() and re-raises non-Exception
            from app.agents.web_search_agent import WebSearchAgent, WebSearchAgentInput

            agent = WebSearchAgent(
                llm_client=self._openrouter,
                search_service=self._topic_search,
                cfg=self._cfg.web_search,
                correlation_id=correlation_id,
            )
            input_data = WebSearchAgentInput(
                content=content_text[:8000],
                language=chosen_lang,
                correlation_id=correlation_id,
            )
            result = await agent.execute(input_data)

            if result.success and result.output and result.output.context:
                context = result.output.context
                logger.info(
                    "web_search_context_injected",
                    extra={
                        "cid": correlation_id,
                        "searched": result.output.searched,
                        "queries": result.output.queries_executed,
                        "articles_found": result.output.articles_found,
                        "context_chars": len(context),
                    },
                )
                current_date = datetime.now(UTC).strftime("%Y-%m-%d")
                return f"ADDITIONAL WEB CONTEXT (retrieved {current_date}):\n{context}"

            return ""
        except BaseException as exc:
            raise_if_cancelled(exc)
            if not isinstance(exc, Exception):  # pragma: no cover - defensive
                raise
            logger.warning(
                "web_search_enrichment_failed",
                extra={"cid": correlation_id, "error": str(exc)},
            )
            record_web_search_decision("failed")
            return ""
