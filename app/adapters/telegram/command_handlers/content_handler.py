"""Content retrieval command handlers (/unread, /read).

This module handles commands for retrieving and managing article content,
including listing unread articles and marking them as read.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from app.adapters.telegram.command_handlers.decorators import audit_command
from app.adapters.telegram.command_handlers.utils import maybe_load_json
from app.application.services.topic_search_utils import ensure_mapping
from app.core.logging_utils import get_logger
from app.application.services.user_interaction_service import async_safe_update_user_interaction

if TYPE_CHECKING:
    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )
    from app.adapters.telegram.command_handlers.execution_context import (
        CommandExecutionContext,
    )
    from app.application.ports.requests import LLMRepositoryPort
    from app.application.ports.summaries import SummaryRepositoryPort

logger = get_logger(__name__)


class ContentHandler:
    """Implementation of content retrieval commands (/unread, /read).

    Handles listing unread articles, viewing article details, and
    marking articles as read.
    """

    def __init__(
        self,
        response_formatter: ResponseFormatter,
        summary_repo: SummaryRepositoryPort,
        llm_repo: LLMRepositoryPort,
        *,
        unread_summaries_use_case: Any | None = None,
        mark_summary_as_read_use_case: Any | None = None,
    ) -> None:
        self._formatter = response_formatter
        self._summary_repo = summary_repo
        self._llm_repo = llm_repo
        self._unread_summaries_use_case = unread_summaries_use_case
        self._mark_summary_as_read_use_case = mark_summary_as_read_use_case

    @staticmethod
    def parse_unread_arguments(text: str | None) -> tuple[int, str | None]:
        """Parse optional limit and topic arguments from an /unread command string.

        Supports formats like:
        - /unread
        - /unread 10
        - /unread tech
        - /unread tech 10
        - /unread limit=10 tech
        - /unread@botname 5 topic

        Args:
            text: The command text to parse.

        Returns:
            Tuple of (limit, topic) where limit defaults to 5 and topic may be None.
        """
        if not text:
            return 5, None

        # Strip command prefix
        remainder = text[len("/unread") :].strip() if text.startswith("/unread") else text
        if not remainder:
            return 5, None

        tokens = remainder.split()

        # Handle bot mention (e.g., @botname)
        had_mention = False
        if tokens and tokens[0].startswith("@"):
            had_mention = True
            tokens = tokens[1:]

        if not tokens:
            return 5, None

        max_limit = 20
        limit = 5
        topic_tokens: list[tuple[str, bool]] = []
        explicit_limit_set = False

        for raw_token in tokens:
            token = raw_token.strip()
            if not token:
                continue

            lowered = token.casefold()

            # Check for explicit limit=N or limit:N format
            if lowered.startswith(("limit=", "limit:")):
                candidate = token.split("=", 1)[-1] if "=" in token else token.split(":", 1)[-1]
                try:
                    parsed = int(candidate)
                except ValueError:
                    topic_tokens.append((token, False))
                    continue
                limit = max(1, min(parsed, max_limit))
                explicit_limit_set = True
                continue

            topic_tokens.append((token, token.isdigit()))

        # If no explicit limit, check if last token is a number (implicit limit)
        if not explicit_limit_set and topic_tokens:
            candidate_index = len(topic_tokens) - 1
            if topic_tokens[candidate_index][1]:
                raw_limit_value = topic_tokens[candidate_index][0]
                try:
                    parsed_limit = int(raw_limit_value)
                except ValueError:
                    logger.debug(
                        "content_query_limit_parse_failed", extra={"raw_value": raw_limit_value}
                    )
                else:
                    if parsed_limit <= max_limit:
                        has_non_digit_before = any(
                            not is_digit for _, is_digit in topic_tokens[:candidate_index]
                        )
                        if had_mention or has_non_digit_before:
                            limit = max(1, min(parsed_limit, max_limit))
                            del topic_tokens[candidate_index]

        topic = " ".join(token for token, _ in topic_tokens).strip() or None
        return limit, topic

    @audit_command("command_unread")
    async def handle_unread(self, ctx: CommandExecutionContext) -> None:
        """Handle /unread command.

        Lists unread articles with optional topic filtering.

        Args:
            ctx: The command execution context.
        """
        limit, topic = self.parse_unread_arguments(ctx.text)

        try:
            unread_summaries = await self._get_unread_summaries(ctx, limit, topic)

            if not unread_summaries:
                if topic:
                    await self._formatter.safe_reply(
                        ctx.message,
                        f'📖 No unread articles found for topic "{topic}".',
                    )
                    return
                await self._formatter.safe_reply(
                    ctx.message, "📖 No unread articles found. All caught up!"
                )
                return

            # Format and send response
            await self._send_unread_list(ctx, unread_summaries, limit, topic)

            if ctx.interaction_id:
                await async_safe_update_user_interaction(
                    ctx.user_repo,
                    interaction_id=ctx.interaction_id,
                    response_sent=True,
                    response_type="unread_list",
                    start_time=ctx.start_time,
                    logger_=logger,
                )

        except Exception as exc:
            logger.exception("command_unread_failed", extra={"cid": ctx.correlation_id})
            await self._formatter.safe_reply(
                ctx.message,
                "⚠️ Unable to retrieve unread articles right now. Check bot logs for details.",
            )
            if ctx.interaction_id:
                await async_safe_update_user_interaction(
                    ctx.user_repo,
                    interaction_id=ctx.interaction_id,
                    response_sent=True,
                    response_type="error",
                    error_occurred=True,
                    error_message=str(exc)[:500],
                    start_time=ctx.start_time,
                    logger_=logger,
                )

    async def _get_unread_summaries(
        self,
        ctx: CommandExecutionContext,
        limit: int,
        topic: str | None,
    ) -> list[dict[str, Any]]:
        """Get unread summaries via the application use case.

        Args:
            ctx: The command execution context.
            limit: Maximum number of summaries to return.
            topic: Optional topic filter.

        Returns:
            List of unread summary dictionaries.
        """
        if self._unread_summaries_use_case is None:
            msg = "ContentHandler requires the unread summaries use case"
            raise RuntimeError(msg)

        from app.application.use_cases.get_unread_summaries import GetUnreadSummariesQuery

        query = GetUnreadSummariesQuery(
            user_id=ctx.uid,
            chat_id=ctx.chat_id,
            limit=limit,
            topic=topic,
        )
        return await self._unread_summaries_use_case.execute(query)

    async def _send_unread_list(
        self,
        ctx: CommandExecutionContext,
        unread_summaries: list[dict[str, Any]],
        limit: int,
        topic: str | None,
    ) -> None:
        """Format and send the unread articles list.

        Args:
            ctx: The command execution context.
            unread_summaries: List of unread summary data.
            limit: The limit that was applied.
            topic: The topic filter that was applied.
        """
        response_lines = ["📚 **Unread Articles:**"]

        if topic:
            response_lines.append(f"🔍 Topic filter: {topic}")
        if limit:
            response_lines.append(f"📦 Showing up to {limit} article(s)")

        for i, summary in enumerate(unread_summaries, 1):
            request_id = summary.get("request_id")
            input_url = summary.get("input_url", "Unknown URL")
            created_at = summary.get("created_at", "Unknown date")

            # Extract title from metadata if available
            payload = maybe_load_json(summary.get("json_payload"))
            if isinstance(payload, Mapping):
                metadata = ensure_mapping(payload.get("metadata"))
                title = metadata.get("title") or payload.get("title") or input_url
            else:
                title = input_url

            response_lines.append(
                f"{i}. **{title}**\n"
                f"   🔗 {input_url}\n"
                f"   📅 {created_at}\n"
                f"   🆔 Request ID: `{request_id}`"
            )

        response_lines.append(
            "\n💡 **Tip:** Send `/read <request_id>` to mark an article as read and view it."
        )

        await self._formatter.safe_reply(ctx.message, "\n".join(response_lines))

    @audit_command("command_read", include_text=True)
    async def handle_read(self, ctx: CommandExecutionContext) -> None:
        """Handle /read <request_id> command.

        Marks an article as read and displays its full summary.

        Args:
            ctx: The command execution context.
        """
        try:
            # Extract request_id from command text
            parts = ctx.text.split()
            if len(parts) < 2:
                await self._formatter.safe_reply(
                    ctx.message, "❌ Usage: `/read <request_id>`\n\nExample: `/read 123`"
                )
                return

            try:
                request_id = int(parts[1])
            except ValueError:
                await self._formatter.safe_reply(
                    ctx.message, "❌ Invalid request ID. Must be a number.\n\nExample: `/read 123`"
                )
                return

            # Get the unread summary
            summary = await self._summary_repo.async_get_unread_summary_by_request_id(request_id)
            if not summary:
                await self._formatter.safe_reply(
                    ctx.message, f"❌ Article with ID `{request_id}` not found or already read."
                )
                return

            # Parse the summary payload
            payload = maybe_load_json(summary.get("json_payload"))
            if isinstance(payload, Mapping):
                shaped = dict(payload)
            else:
                shaped = {}
                if payload is not None:
                    await self._formatter.safe_reply(
                        ctx.message,
                        f"❌ Error reading article data for ID `{request_id}`.",
                    )
                    return

            # Mark as read
            await self._mark_as_read(ctx, summary, request_id)

            # Send the article
            input_url = summary.get("input_url", "Unknown URL")
            await self._formatter.safe_reply(
                ctx.message, f"📖 **Reading Article** (ID: `{request_id}`)\n🔗 {input_url}"
            )

            # Send the summary
            if shaped:
                # Resolve model used for this request
                try:
                    model_name = await self._llm_repo.async_get_latest_llm_model_by_request_id(
                        request_id
                    )
                except Exception:
                    model_name = None
                llm_stub = type("LLMStub", (), {"model": model_name})()
                await self._formatter.send_structured_summary_response(
                    ctx.message, shaped, llm_stub
                )

            # Send additional insights if available
            insights_raw = summary.get("insights_json")
            insights = maybe_load_json(insights_raw)
            if isinstance(insights, Mapping) and insights:
                await self._formatter.send_additional_insights_message(
                    ctx.message, dict(insights), ctx.correlation_id
                )

            if ctx.interaction_id:
                await async_safe_update_user_interaction(
                    ctx.user_repo,
                    interaction_id=ctx.interaction_id,
                    response_sent=True,
                    response_type="read_article",
                    request_id=request_id,
                    start_time=ctx.start_time,
                    logger_=logger,
                )

        except Exception as exc:
            logger.exception("command_read_failed", extra={"cid": ctx.correlation_id})
            await self._formatter.safe_reply(
                ctx.message,
                "⚠️ Unable to read the article right now. Check bot logs for details.",
            )
            if ctx.interaction_id:
                await async_safe_update_user_interaction(
                    ctx.user_repo,
                    interaction_id=ctx.interaction_id,
                    response_sent=True,
                    response_type="error",
                    error_occurred=True,
                    error_message=str(exc)[:500],
                    start_time=ctx.start_time,
                    logger_=logger,
                )

    async def _mark_as_read(
        self,
        ctx: CommandExecutionContext,
        summary: dict[str, Any],
        request_id: int,
    ) -> None:
        """Mark an article as read via the application use case.

        Args:
            ctx: The command execution context.
            summary: The summary data dictionary.
            request_id: The request ID.
        """
        if self._mark_summary_as_read_use_case is None:
            msg = "ContentHandler requires read-marking application services"
            raise RuntimeError(msg)

        from app.application.use_cases.mark_summary_as_read import MarkSummaryAsReadCommand

        summary_id = summary.get("id")
        if not summary_id:
            msg = f"Cannot mark as read for request_id={request_id}: missing summary id"
            raise RuntimeError(msg)

        command = MarkSummaryAsReadCommand(
            summary_id=summary_id,
            user_id=ctx.uid,
        )
        await self._mark_summary_as_read_use_case.execute(command)
