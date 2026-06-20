"""Search command handlers (/find*, /search).

This module handles all search-related commands including online topic search,
local database search, and hybrid semantic search.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from app.adapters.telegram.command_handlers.decorators import audit_command
from app.application.services.user_interaction_service import async_safe_update_user_interaction
from app.core.async_utils import raise_if_cancelled
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )
    from app.adapters.telegram.command_handlers.execution_context import (
        CommandExecutionContext,
    )
    from app.application.services.topic_search import LocalTopicSearchService, TopicSearchService
    from app.infrastructure.search.hybrid_search_service import HybridSearchService


class SearcherProvider(Protocol):
    """Protocol for objects that supply the three search backends."""

    topic_searcher: TopicSearchService | None
    local_searcher: LocalTopicSearchService | None
    hybrid_search: HybridSearchService | None


logger = get_logger(__name__)


class SearchHandler:
    """Implementation of search commands (/find*, /search).

    Handles multiple search modes:
    - Online search via Firecrawl (/findweb, /find, /findonline)
    - Local database search (/finddb, /findlocal)
    - Hybrid semantic + keyword search (/search)
    """

    def __init__(
        self,
        response_formatter: ResponseFormatter,
        searcher_provider: SearcherProvider,
        *,
        search_topics_use_case: Any | None = None,
    ) -> None:
        self._formatter = response_formatter
        self._searcher_provider = searcher_provider
        self._search_topics_use_case = search_topics_use_case

    @property
    def _topic_searcher(self) -> TopicSearchService | None:
        return self._searcher_provider.topic_searcher

    @property
    def _local_searcher(self) -> LocalTopicSearchService | None:
        return self._searcher_provider.local_searcher

    @property
    def _hybrid_search(self) -> HybridSearchService | None:
        return self._searcher_provider.hybrid_search

    async def handle_find_online(
        self,
        ctx: CommandExecutionContext,
        *,
        command: str,
    ) -> None:
        """Handle Firecrawl-backed search commands (/findweb, /find, /findonline).

        Searches for articles online using Firecrawl service.

        Args:
            ctx: The command execution context.
            command: The command that triggered this handler (for usage examples).
        """
        await self._handle_topic_search(
            ctx,
            command=command,
            searcher=self._topic_searcher,
            unavailable_message="⚠️ Online article search is currently unavailable.",
            usage_example="❌ Usage: `{cmd} <topic>`\n\nExample: `{cmd} Android System Design`",
            invalid_message="❌ Topic must contain visible characters. Try `{cmd} space exploration`.",
            error_message="⚠️ Unable to search online articles right now. Please try again later.",
            empty_message="No recent online articles found for **{topic}**.",
            response_prefix="topic_search_online",
            log_event="command_find_online",
            formatter_source="online",
        )

    async def handle_find_local(
        self,
        ctx: CommandExecutionContext,
        *,
        command: str,
    ) -> None:
        """Handle database-only topic search commands (/finddb, /findlocal).

        Searches for articles in the local database.

        Args:
            ctx: The command execution context.
            command: The command that triggered this handler (for usage examples).
        """
        await self._handle_topic_search(
            ctx,
            command=command,
            searcher=self._local_searcher,
            unavailable_message="⚠️ Library search is currently unavailable.",
            usage_example="❌ Usage: `{cmd} <topic>`\n\nExample: `{cmd} Android System Design`",
            invalid_message="❌ Topic must contain visible characters. Try `{cmd} space exploration`.",
            error_message="⚠️ Unable to search saved articles right now. Please try again later.",
            empty_message="No saved summaries matched **{topic}**.",
            response_prefix="topic_search_local",
            log_event="command_find_local",
            formatter_source="library",
        )

    async def _handle_topic_search(
        self,
        ctx: CommandExecutionContext,
        *,
        command: str,
        searcher: TopicSearchService | LocalTopicSearchService | None,
        unavailable_message: str,
        usage_example: str,
        invalid_message: str,
        error_message: str,
        empty_message: str,
        response_prefix: str,
        log_event: str,
        formatter_source: str,
    ) -> None:
        """Shared topic search implementation.

        Args:
            ctx: The command execution context.
            command: The command name for error messages.
            searcher: The search service to use.
            unavailable_message: Message when service is unavailable.
            usage_example: Usage example template.
            invalid_message: Message for invalid topic.
            error_message: Message for search errors.
            empty_message: Message when no results found.
            response_prefix: Prefix for interaction response types.
            log_event: Event name for logging.
            formatter_source: Source identifier for response formatting.
        """
        # Log the command
        logger.info(
            log_event,
            extra={
                "uid": ctx.uid,
                "chat_id": ctx.chat_id,
                "cid": ctx.correlation_id,
                "text": ctx.text[:100],
            },
        )
        try:
            ctx.audit_func(
                "INFO",
                log_event,
                {
                    "uid": ctx.uid,
                    "chat_id": ctx.chat_id,
                    "cid": ctx.correlation_id,
                    "text": ctx.text[:100],
                },
            )
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.warning("audit_log_failed", extra={"error": str(exc)})

        # Extract topic from command text
        parts = ctx.text.split(maxsplit=1)
        topic = parts[1].strip() if len(parts) > 1 else ""

        if not topic:
            usage = usage_example.format(cmd=command)
            await self._formatter.safe_reply(ctx.message, usage)
            if ctx.interaction_id:
                await async_safe_update_user_interaction(
                    ctx.user_repo,
                    interaction_id=ctx.interaction_id,
                    response_sent=True,
                    response_type=f"{response_prefix}_usage",
                    start_time=ctx.start_time,
                    logger_=logger,
                )
            return

        # Check if searcher is available after validating command usage.
        if not searcher:
            await self._formatter.safe_reply(ctx.message, unavailable_message)
            if ctx.interaction_id:
                await async_safe_update_user_interaction(
                    ctx.user_repo,
                    interaction_id=ctx.interaction_id,
                    response_sent=True,
                    response_type=f"{response_prefix}_disabled",
                    start_time=ctx.start_time,
                    logger_=logger,
                )
            return

        try:
            results = await self._execute_topic_search(ctx, searcher, topic, formatter_source)
        except ValueError:
            invalid = invalid_message.format(cmd=command)
            await self._formatter.safe_reply(ctx.message, invalid)
            if ctx.interaction_id:
                await async_safe_update_user_interaction(
                    ctx.user_repo,
                    interaction_id=ctx.interaction_id,
                    response_sent=True,
                    response_type=f"{response_prefix}_invalid",
                    start_time=ctx.start_time,
                    logger_=logger,
                )
            return
        except Exception as exc:
            logger.exception(f"{log_event}_failed", extra={"cid": ctx.correlation_id})
            await self._formatter.safe_reply(ctx.message, error_message)
            if ctx.interaction_id:
                await async_safe_update_user_interaction(
                    ctx.user_repo,
                    interaction_id=ctx.interaction_id,
                    response_sent=True,
                    response_type=f"{response_prefix}_error",
                    error_occurred=True,
                    error_message=str(exc)[:500],
                    start_time=ctx.start_time,
                    logger_=logger,
                )
            return

        if not results:
            await self._formatter.safe_reply(ctx.message, empty_message.format(topic=topic))
            if ctx.interaction_id:
                await async_safe_update_user_interaction(
                    ctx.user_repo,
                    interaction_id=ctx.interaction_id,
                    response_sent=True,
                    response_type=f"{response_prefix}_empty",
                    start_time=ctx.start_time,
                    logger_=logger,
                )
            return

        await self._formatter.send_topic_search_results(
            ctx.message,
            topic=topic,
            articles=results,
            source=formatter_source,
        )
        if ctx.interaction_id:
            await async_safe_update_user_interaction(
                ctx.user_repo,
                interaction_id=ctx.interaction_id,
                response_sent=True,
                response_type=f"{response_prefix}_results",
                start_time=ctx.start_time,
                logger_=logger,
            )

    async def _execute_topic_search(
        self,
        ctx: CommandExecutionContext,
        searcher: TopicSearchService | LocalTopicSearchService,
        topic: str,
        formatter_source: str,
    ) -> list[Any]:
        """Execute topic search via application use case (library) or service (online).

        Args:
            ctx: The command execution context.
            searcher: The search service to use.
            topic: The topic to search for.
            formatter_source: Source identifier for choosing search path.

        Returns:
            List of search results.
        """
        if formatter_source == "library":
            # Prefer local service direct call for compatibility with tests and
            # lightweight in-memory adapters.
            find_articles = getattr(searcher, "find_articles", None)
            if callable(find_articles):
                return await find_articles(topic, correlation_id=ctx.correlation_id)

            if self._search_topics_use_case is None:
                msg = "SearchHandler requires the search topics use case for library search"
                raise RuntimeError(msg)
            from app.application.use_cases.search_topics import SearchTopicsQuery

            query = SearchTopicsQuery(
                topic=topic,
                user_id=ctx.uid,
                max_results=getattr(searcher, "max_results", 5),
                correlation_id=ctx.correlation_id,
            )
            return await self._search_topics_use_case.execute(query)

        # Online search remains a direct adapter/service call.
        return await searcher.find_articles(topic, correlation_id=ctx.correlation_id)

    @audit_command("command_search", include_text=True)
    async def handle_search(self, ctx: CommandExecutionContext) -> None:
        """Handle /search command - hybrid semantic + keyword search.

        Performs advanced search combining vector similarity and keyword matching.

        Args:
            ctx: The command execution context.
        """
        # Check if search service is available
        if not self._hybrid_search:
            await self._formatter.safe_reply(
                ctx.message, "⚠️ Semantic search is currently unavailable."
            )
            if ctx.interaction_id:
                await async_safe_update_user_interaction(
                    ctx.user_repo,
                    interaction_id=ctx.interaction_id,
                    response_sent=True,
                    response_type="search_disabled",
                    start_time=ctx.start_time,
                    logger_=logger,
                )
            return

        # Extract query from command
        parts = ctx.text.split(maxsplit=1)
        query = parts[1].strip() if len(parts) > 1 else ""

        if not query:
            usage_msg = (
                "❌ Usage: `/search <query>`\n\n"
                "**Examples:**\n"
                "• `/search machine learning`\n"
                "• `/search python async programming`\n"
                "• `/search AI ethics`\n\n"
                "💡 **Features:**\n"
                "• Semantic vector search\n"
                "• Keyword (FTS) search\n"
                "• Query expansion with synonyms\n"
                "• Hybrid scoring for best results"
            )
            await self._formatter.safe_reply(ctx.message, usage_msg)
            if ctx.interaction_id:
                await async_safe_update_user_interaction(
                    ctx.user_repo,
                    interaction_id=ctx.interaction_id,
                    response_sent=True,
                    response_type="search_usage",
                    start_time=ctx.start_time,
                    logger_=logger,
                )
            return

        # Send searching message
        await self._formatter.safe_reply(ctx.message, f"🔍 Searching for: **{query}**...")

        try:
            # Perform hybrid search
            results = await self._hybrid_search.search(
                query=query,
                correlation_id=ctx.correlation_id,
            )

            if not results:
                await self._formatter.safe_reply(
                    ctx.message,
                    f"📭 No articles found for **{query}**.\n\n"
                    "💡 Try:\n"
                    "• Broader search terms\n"
                    "• Different keywords\n"
                    "• Check `/find` for online search",
                )
                if ctx.interaction_id:
                    await async_safe_update_user_interaction(
                        ctx.user_repo,
                        interaction_id=ctx.interaction_id,
                        response_sent=True,
                        response_type="search_empty",
                        start_time=ctx.start_time,
                        logger_=logger,
                    )
                return

            # Format and send results
            await self._send_search_results(ctx.message, query, results)

            if ctx.interaction_id:
                await async_safe_update_user_interaction(
                    ctx.user_repo,
                    interaction_id=ctx.interaction_id,
                    response_sent=True,
                    response_type="search_results",
                    start_time=ctx.start_time,
                    logger_=logger,
                )

        except Exception as exc:
            logger.exception("command_search_failed", extra={"cid": ctx.correlation_id})
            await self._formatter.safe_reply(
                ctx.message,
                "⚠️ Search failed. Please try again later or check bot logs for details.",
            )
            if ctx.interaction_id:
                await async_safe_update_user_interaction(
                    ctx.user_repo,
                    interaction_id=ctx.interaction_id,
                    response_sent=True,
                    response_type="search_error",
                    error_occurred=True,
                    error_message=str(exc)[:500],
                    start_time=ctx.start_time,
                    logger_=logger,
                )

    async def _send_search_results(
        self,
        message: Any,
        query: str,
        results: list[Any],
    ) -> None:
        """Format and send search results to user.

        Args:
            message: The Telegram message to reply to.
            query: The search query.
            results: List of search results.
        """
        response_lines = [
            f"🎯 **Search Results** for: **{query}**",
            f"📊 Found {len(results)} article(s)\n",
        ]

        for i, result in enumerate(results[:10], 1):  # Limit to top 10 for Telegram
            title = result.title or result.url or "Untitled"
            url = result.url or ""
            snippet = result.snippet or ""

            # Truncate long titles and snippets
            if len(title) > 100:
                title = title[:97] + "..."
            if len(snippet) > 150:
                snippet = snippet[:147] + "..."

            result_text = f"{i}. **{title}**"
            if url:
                result_text += f"\n   🔗 {url}"
            if snippet:
                result_text += f"\n   📝 {snippet}"

            # Add source and date if available
            metadata_parts = []
            if result.source:
                metadata_parts.append(f"📰 {result.source}")
            if result.published_at:
                metadata_parts.append(f"📅 {result.published_at}")
            if metadata_parts:
                result_text += f"\n   {' | '.join(metadata_parts)}"

            response_lines.append(result_text)

        response_lines.append("\n💡 **Tip:** Use `/read <request_id>` to view full summaries")

        await self._formatter.safe_reply(message, "\n\n".join(response_lines))
