"""Search context builder for formatting web search results for LLM injection."""

from __future__ import annotations

from datetime import UTC
from typing import TYPE_CHECKING

from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from app.application.services.topic_search import TopicArticle

logger = get_logger(__name__)


class SearchContextBuilder:
    """Builds formatted context from search results for LLM injection.

    This class takes search results (TopicArticle objects) and formats them
    as markdown suitable for injecting into LLM prompts as additional context.

    Features:
    - Deduplication by URL
    - Character limit enforcement with graceful truncation
    - Attribution with title, source, and date
    - Markdown formatting for readability
    """

    def __init__(self, max_chars: int = 2000):
        if max_chars < 100:
            raise ValueError("max_chars must be at least 100")
        self.max_chars = max_chars

    def build_context(self, articles: list[TopicArticle]) -> str:
        """Build markdown context from search results.

        Args:
            articles: List of TopicArticle objects from search

        Returns:
            Formatted markdown context string, deduplicated and truncated
        """
        if not articles:
            return ""

        # Deduplicate by URL
        seen_urls: set[str] = set()
        unique_articles: list[TopicArticle] = []

        for article in articles:
            if article.url not in seen_urls:
                seen_urls.add(article.url)
                unique_articles.append(article)

        # Build formatted entries
        entries: list[str] = []
        total_chars = 0
        buffer_chars = 50  # Reserve space for section separators

        for article in unique_articles:
            entry = self._format_article(article)
            entry_len = len(entry)

            # Check if adding this entry would exceed limit
            if total_chars + entry_len + buffer_chars > self.max_chars:
                # Try to fit a truncated version
                remaining = self.max_chars - total_chars - buffer_chars
                if remaining > 150:  # Only add if we can show meaningful content
                    truncated = self._format_article(article, max_snippet_len=remaining - 80)
                    if truncated:
                        entries.append(truncated)
                break

            entries.append(entry)
            total_chars += entry_len + 2  # Account for separators

        if not entries:
            return ""

        return "\n\n".join(entries)

    def build_context_with_header(
        self, articles: list[TopicArticle], header: str | None = None
    ) -> str:
        """Build context with an optional header.

        Args:
            articles: List of TopicArticle objects from search
            header: Optional header text (default uses standard header)

        Returns:
            Formatted context with header
        """
        context = self.build_context(articles)
        if not context:
            return ""

        if header is None:
            from datetime import datetime

            header = f"ADDITIONAL WEB CONTEXT (retrieved {datetime.now(UTC).strftime('%Y-%m-%d')}):"

        return f"{header}\n{context}"

    def _format_article(self, article: TopicArticle, max_snippet_len: int | None = None) -> str:
        """Format a single article as markdown.

        Args:
            article: TopicArticle to format
            max_snippet_len: Optional maximum length for snippet

        Returns:
            Formatted markdown string
        """
        parts: list[str] = []

        # Title (bold)
        title = (article.title or "Untitled").strip()
        if len(title) > 100:
            title = title[:97] + "..."
        parts.append(f"**{title}**")

        # Source and date metadata
        meta_parts: list[str] = []
        if article.source:
            source = article.source.strip()
            if len(source) > 50:
                source = source[:47] + "..."
            meta_parts.append(source)
        if article.published_at:
            meta_parts.append(article.published_at.strip())
        if meta_parts:
            parts.append(f"*{' | '.join(meta_parts)}*")

        # Snippet content
        if article.snippet:
            snippet = article.snippet.strip()
            if max_snippet_len and len(snippet) > max_snippet_len:
                # Try to break at sentence boundary
                truncated = snippet[:max_snippet_len]
                last_period = truncated.rfind(". ")
                if last_period > max_snippet_len // 2:
                    snippet = truncated[: last_period + 1]
                else:
                    snippet = truncated.rstrip() + "..."
            parts.append(snippet)

        return "\n".join(parts)
