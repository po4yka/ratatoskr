"""Use case for searching topics and finding related articles."""

from dataclasses import dataclass
from typing import Any

from app.application.use_cases._tracing import use_case_span
from app.core.logging_utils import get_logger

logger = get_logger(__name__)


@dataclass
class TopicArticleDTO:
    """Data transfer object for topic article results."""

    title: str
    url: str
    snippet: str | None = None
    source: str | None = None
    published_at: str | None = None


@dataclass
class SearchTopicsQuery:
    """Query for searching topics."""

    topic: str
    user_id: int
    max_results: int = 5
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        if not self.topic or not self.topic.strip():
            msg = "topic must not be empty"
            raise ValueError(msg)
        if self.user_id <= 0:
            msg = "user_id must be positive"
            raise ValueError(msg)
        if self.max_results <= 0:
            msg = "max_results must be positive"
            raise ValueError(msg)
        if self.max_results > 10:
            msg = "max_results cannot exceed 10"
            raise ValueError(msg)


class SearchTopicsUseCase:
    """Use case for searching topics and discovering articles."""

    def __init__(self, topic_search_service: Any) -> None:
        self._search_service = topic_search_service

    async def execute(self, query: SearchTopicsQuery) -> list[TopicArticleDTO]:
        """Execute the topic search query.

        Args:
            query: Query parameters including topic and search options.

        Returns:
            List of TopicArticleDTO objects representing found articles.

        Raises:
            ValueError: If topic is invalid.
            Exception: If search service fails.

        """
        with use_case_span("search_topics.execute", query):
            logger.info(
                "search_topics_started",
                extra={
                    "topic": query.topic,
                    "user_id": query.user_id,
                    "max_results": query.max_results,
                    "cid": query.correlation_id,
                },
            )

            try:
                articles = await self._search_service.find_articles(
                    topic=query.topic,
                    correlation_id=query.correlation_id,
                )

                result = [
                    TopicArticleDTO(
                        title=article.title,
                        url=article.url,
                        snippet=article.snippet,
                        source=article.source,
                        published_at=article.published_at,
                    )
                    for article in articles
                ]

                logger.info(
                    "search_topics_completed",
                    extra={
                        "topic": query.topic,
                        "user_id": query.user_id,
                        "count": len(result),
                        "cid": query.correlation_id,
                    },
                )

                return result

            except ValueError as e:
                logger.warning(
                    "search_topics_validation_error",
                    extra={
                        "topic": query.topic,
                        "error": str(e),
                        "cid": query.correlation_id,
                    },
                )
                raise

            except Exception as e:
                logger.exception(
                    "search_topics_failed",
                    extra={
                        "topic": query.topic,
                        "error": str(e),
                        "cid": query.correlation_id,
                    },
                )
                raise
