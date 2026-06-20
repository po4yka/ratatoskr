"""Tag management command handlers (/tag, /untag, /tags).

Lets users manage tags on summaries via Telegram reply commands.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.adapters.telegram.command_handlers.base_handler import HandlerDependenciesMixin
from app.adapters.telegram.command_handlers.decorators import combined_handler
from app.core.logging_utils import get_logger
from app.domain.services.tag_service import normalize_tag_name, validate_tag_name

if TYPE_CHECKING:
    from app.adapters.telegram.command_handlers.execution_context import (
        CommandExecutionContext,
    )
    from app.application.ports.requests import RequestRepositoryPort
    from app.application.ports.summaries import SummaryRepositoryPort, TagRepositoryPort

logger = get_logger(__name__)

_MAX_TAG_SUMMARIES = 10


class TagHandler(HandlerDependenciesMixin):
    """Handle /tag, /untag, and /tags commands."""

    def __init__(
        self,
        *,
        cfg: Any,
        db: Any,
        response_formatter: Any,
        tag_repo: TagRepositoryPort,
        request_repo: RequestRepositoryPort,
        summary_repo: SummaryRepositoryPort,
    ) -> None:
        super().__init__(cfg=cfg, db=db, response_formatter=response_formatter)
        self._tag_repo = tag_repo
        self._request_repo = request_repo
        self._summary_repo = summary_repo

    @combined_handler("command_tag", "tag")
    async def handle_tag(self, ctx: CommandExecutionContext) -> None:
        """Handle /tag <name> -- add a tag to the replied-to summary."""
        tag_name = _parse_tag_arg(ctx.text, "/tag")
        if not tag_name:
            await ctx.response_formatter.safe_reply(
                ctx.message,
                "Usage: reply to a summary with /tag <name>",
            )
            return

        valid, error = validate_tag_name(tag_name)
        if not valid:
            await ctx.response_formatter.safe_reply(ctx.message, f"Invalid tag: {error}")
            return

        summary = await _find_summary_from_reply(
            ctx,
            request_repo=self._request_repo,
            summary_repo=self._summary_repo,
        )
        if summary is None:
            return  # helper already replied with an error

        normalized = normalize_tag_name(tag_name)

        tag = await self._tag_repo.async_get_tag_by_normalized_name(
            ctx.uid,
            normalized,
            include_deleted=True,
        )
        if tag is None:
            tag = await self._tag_repo.async_create_tag(
                user_id=ctx.uid,
                name=tag_name.strip(),
                normalized_name=normalized,
                color=None,
            )
        elif tag.get("is_deleted"):
            tag = await self._tag_repo.async_restore_tag(
                tag["id"], user_id=ctx.uid, name=tag_name.strip()
            )

        await self._tag_repo.async_attach_tag(summary["id"], tag["id"], "manual")

        await ctx.response_formatter.safe_reply(
            ctx.message,
            f"Tagged with #{normalized}",
        )

    @combined_handler("command_untag", "untag")
    async def handle_untag(self, ctx: CommandExecutionContext) -> None:
        """Handle /untag <name> -- remove a tag from the replied-to summary."""
        tag_name = _parse_tag_arg(ctx.text, "/untag")
        if not tag_name:
            await ctx.response_formatter.safe_reply(
                ctx.message,
                "Usage: reply to a summary with /untag <name>",
            )
            return

        summary = await _find_summary_from_reply(
            ctx,
            request_repo=self._request_repo,
            summary_repo=self._summary_repo,
        )
        if summary is None:
            return

        normalized = normalize_tag_name(tag_name)

        tag = await self._tag_repo.async_get_tag_by_normalized_name(ctx.uid, normalized)
        if tag is None:
            await ctx.response_formatter.safe_reply(
                ctx.message,
                f"Tag #{normalized} not found.",
            )
            return

        tags_for_summary = await self._tag_repo.async_get_tags_for_summary(summary["id"])
        if not any(item.get("id") == tag["id"] for item in tags_for_summary):
            await ctx.response_formatter.safe_reply(
                ctx.message,
                f"This summary is not tagged with #{normalized}.",
            )
            return

        await self._tag_repo.async_detach_tag(summary["id"], tag["id"])
        await ctx.response_formatter.safe_reply(
            ctx.message,
            f"Removed tag #{normalized}",
        )

    @combined_handler("command_tags", "tags")
    async def handle_tags(self, ctx: CommandExecutionContext) -> None:
        """Handle /tags [name].

        No arguments: list all user tags with counts.
        With argument: list summaries for that tag.
        """
        tag_name = _parse_tag_arg(ctx.text, "/tags")

        if tag_name:
            await self._list_tag_summaries(ctx, tag_name)
        else:
            await self._list_all_tags(ctx)

    async def _list_all_tags(self, ctx: CommandExecutionContext) -> None:
        """List all user tags with summary counts."""
        tags = await self._tag_repo.async_get_user_tags(ctx.uid)

        lines: list[str] = []
        for row in tags:
            lines.append(f"#{row['normalized_name']} ({row.get('summary_count', 0)})")

        if not lines:
            await ctx.response_formatter.safe_reply(
                ctx.message,
                "No tags yet. Reply to a summary with /tag <name> to create one.",
            )
            return

        text = "Your tags:\n" + "\n".join(lines)
        await ctx.response_formatter.safe_reply(ctx.message, text)

    async def _list_tag_summaries(self, ctx: CommandExecutionContext, tag_name: str) -> None:
        """List summaries for a specific tag."""
        normalized = normalize_tag_name(tag_name)

        tag = await self._tag_repo.async_get_tag_by_normalized_name(ctx.uid, normalized)
        if tag is None:
            await ctx.response_formatter.safe_reply(
                ctx.message,
                f"Tag #{normalized} not found.",
            )
            return

        summaries = await self._tag_repo.async_get_tagged_summaries(
            user_id=ctx.uid,
            tag_id=tag["id"],
            limit=_MAX_TAG_SUMMARIES,
        )

        lines: list[str] = []
        for s in summaries:
            payload = s.get("json_payload") or {}
            title = payload.get("title", "Untitled")
            request = s.get("request") or {}
            url = request.get("input_url") or request.get("normalized_url") or ""
            if url:
                lines.append(f"- {title}\n  {url}")
            else:
                lines.append(f"- {title}")

        if not lines:
            await ctx.response_formatter.safe_reply(
                ctx.message,
                f"No summaries tagged with #{normalized}.",
            )
            return

        text = f"Summaries tagged #{normalized}:\n\n" + "\n".join(lines)
        await ctx.response_formatter.safe_reply(ctx.message, text)


def _parse_tag_arg(text: str, command: str) -> str | None:
    """Extract the argument after the command prefix, e.g. '/tag ml' -> 'ml'."""
    rest = text[len(command) :].strip()
    return rest if rest else None


async def _find_summary_from_reply(
    ctx: CommandExecutionContext,
    *,
    request_repo: RequestRepositoryPort,
    summary_repo: SummaryRepositoryPort,
) -> dict[str, Any] | None:
    """Look up the summary from the message the user replied to.

    Returns None and sends an error reply if no summary is found.
    """
    reply = getattr(ctx.message, "reply_to_message", None)
    if reply is None:
        await ctx.response_formatter.safe_reply(
            ctx.message,
            "Reply to a summary message to use this command.",
        )
        return None

    reply_msg_id = getattr(reply, "id", None) or getattr(reply, "message_id", None)
    if reply_msg_id is None:
        await ctx.response_formatter.safe_reply(
            ctx.message,
            "Could not identify the replied message.",
        )
        return None

    request = await request_repo.async_get_request_by_telegram_message(
        user_id=ctx.uid,
        message_id=reply_msg_id,
    )
    summary = (
        await summary_repo.async_get_summary_by_request(int(request["id"]))
        if request is not None
        else None
    )

    if summary is None:
        await ctx.response_formatter.safe_reply(
            ctx.message,
            "No summary found for that message. Reply to a summary message.",
        )
        return None

    return summary
