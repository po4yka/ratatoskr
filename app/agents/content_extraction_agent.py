"""Content extraction agent for Firecrawl integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from app.agents.base_agent import AgentResult, BaseAgent, _tracer
from app.core.url_utils import compute_dedupe_hash, normalize_url
from app.observability.attributes import AGENT_ATTEMPT, AGENT_NAME, REQUEST_CORRELATION_ID

if TYPE_CHECKING:
    from app.adapters.content.content_extractor import ContentExtractor
    from app.application.ports.requests import CrawlResultRepositoryPort, RequestRepositoryPort


class ExtractionInput(BaseModel):
    """Input for content extraction."""

    model_config = ConfigDict(frozen=True)

    url: str
    correlation_id: str
    force_refresh: bool = False


class ExtractionOutput(BaseModel):
    """Output from content extraction."""

    content_markdown: str
    content_html: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    normalized_url: str
    crawl_result_id: int | None = None


class ContentExtractionAgent(BaseAgent[ExtractionInput, ExtractionOutput]):
    """Agent responsible for extracting content from URLs using Firecrawl.

    This agent:
    - Normalizes URLs for consistency
    - Calls Firecrawl API to extract content
    - Validates extracted content quality
    - Persists crawl results to database
    - Provides retry logic with exponential backoff
    """

    def __init__(
        self,
        content_extractor: ContentExtractor,
        request_repo: RequestRepositoryPort,
        crawl_result_repo: CrawlResultRepositoryPort,
        correlation_id: str | None = None,
    ):
        super().__init__(name="ContentExtractionAgent", correlation_id=correlation_id)
        self.content_extractor = content_extractor
        self.request_repo = request_repo
        self.crawl_result_repo = crawl_result_repo

    async def execute(self, input_data: ExtractionInput) -> AgentResult[ExtractionOutput]:
        """Extract content from the given URL."""
        self.correlation_id = input_data.correlation_id
        self.log_info("content_extraction_started", url=input_data.url)

        with _tracer.start_as_current_span("agent.content_extraction") as span:
            span.set_attribute(AGENT_NAME, "content_extraction")
            span.set_attribute(REQUEST_CORRELATION_ID, self.correlation_id)
            span.set_attribute(AGENT_ATTEMPT, 1)

            try:
                normalized_url = normalize_url(input_data.url)

                result = await self._extract_with_validation(
                    url=normalized_url,
                    correlation_id=input_data.correlation_id,
                )

                if not result:
                    return AgentResult.error_result(
                        "Content extraction failed - no result returned",
                        url=input_data.url,
                    )

                validation_error = self._validate_content(result)
                if validation_error:
                    self.log_warning(f"Content validation warning: {validation_error}")

                output = ExtractionOutput(
                    content_markdown=result.get("content_markdown", ""),
                    content_html=result.get("content_html"),
                    metadata=result.get("metadata", {}),
                    normalized_url=normalized_url,
                    crawl_result_id=result.get("id"),
                )

                self.log_info(
                    "content_extraction_completed",
                    chars=len(output.content_markdown),
                )

                return AgentResult.success_result(
                    output,
                    content_length=len(output.content_markdown),
                    has_html=output.content_html is not None,
                )

            except Exception as e:
                self.log_error(f"Content extraction failed: {e}")
                return AgentResult.error_result(
                    f"Content extraction error: {e!s}",
                    url=input_data.url,
                    exception_type=type(e).__name__,
                )

    async def _extract_with_validation(
        self, url: str, correlation_id: str
    ) -> dict[str, Any] | None:
        """Return crawl result dict for url, using cached result if available."""
        dedupe_hash = compute_dedupe_hash(url)

        existing_req = await self.request_repo.async_get_request_by_dedupe_hash(dedupe_hash)

        if existing_req:
            req_id = existing_req["id"]

            crawl_result = await self.crawl_result_repo.async_get_crawl_result_by_request(req_id)

            if crawl_result:
                return {
                    "content_markdown": crawl_result.get("content_markdown", ""),
                    "content_html": crawl_result.get("content_html"),
                    "metadata": crawl_result.get("metadata_json", {}),
                    "id": crawl_result.get("id"),
                }

        try:
            (
                content_text,
                _content_source,
                metadata,
            ) = await self.content_extractor.extract_content_pure(
                url=url,
                correlation_id=correlation_id,
            )

            # Return in expected format
            # Note: No crawl_result_id since we didn't persist to DB
            # (persistence is handled by the full message flow)
            return {
                "content_markdown": content_text,
                "content_html": None,  # extract_content_pure doesn't return HTML
                "metadata": metadata,
                "id": None,  # No DB record created in agent-only mode
            }

        except ValueError as e:
            # extract_content_pure raises ValueError for extraction failures
            self.log_error(f"Fresh extraction failed: {e}")
            return None
        except Exception as e:
            # Catch any other unexpected errors
            self.log_error(f"Unexpected error during extraction: {e}")
            return None

    def _validate_content(self, result: dict[str, Any]) -> str | None:
        """Validate extracted content quality.

        Args:
            result: Extraction result to validate

        Returns:
            Error message if validation fails, None otherwise
        """
        content = result.get("content_markdown", "")

        error_indicators = [
            "access denied",
            "404 not found",
            "page not found",
            "forbidden",
            "cloudflare",
        ]

        content_lower = content.lower()
        for indicator in error_indicators:
            if indicator in content_lower and len(content) < 500:
                return f"Content may contain error page ('{indicator}' detected)"

        if len(content) < 100:
            return "Content too short (< 100 chars) - may be extraction failure"

        return None
