"""Telegram command: /star -- star a GitHub repository and file it into a list.

This is deliberately a command rather than a change to how a pasted GitHub URL is
handled. A bare link in the chat keeps producing metadata only, because forwarding
a link is not consent to write to the user's GitHub account; typing ``/star`` is.
The ingestion design document states that boundary explicitly, and this handler
exists to give the write path a gesture of its own rather than to move it.

``add_repository_factory`` is a zero-argument callable injected by the DI layer.
Keeping the factory outside this module avoids a runtime import from the telegram
adapter into the composition root.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.adapters.telegram.command_handlers.base_handler import HandlerDependenciesMixin
from app.adapters.telegram.command_handlers.decorators import combined_handler
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )
    from app.adapters.telegram.command_handlers.execution_context import (
        CommandExecutionContext,
    )
    from app.config import AppConfig
    from app.db.session import Database

logger = get_logger(__name__)

_USAGE = (
    "Usage: /star <github-url>  or  /star owner/name\n"
    "Example: /star torvalds/linux\n"
    "The repository is starred, filed into one of your star lists automatically, "
    "and enrolled for backup."
)


class StarRepositoryHandler(HandlerDependenciesMixin):
    """Handle /star: ingest, star, auto-file and enrol a GitHub repository."""

    def __init__(
        self,
        cfg: AppConfig,
        db: Database,
        response_formatter: ResponseFormatter,
        add_repository_factory: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__(cfg=cfg, db=db, response_formatter=response_formatter)
        self._add_repository_factory = add_repository_factory

    @combined_handler("command_star", "star", include_text=True)
    async def handle_star(self, ctx: CommandExecutionContext) -> None:
        """Handle /star <url-or-owner/name>."""
        raw_arg = ctx.text[len("/star") :].strip()
        if not raw_arg:
            await ctx.response_formatter.safe_reply(ctx.message, _USAGE)
            return

        if self._add_repository_factory is None:
            await ctx.response_formatter.safe_reply(
                ctx.message,
                "Starring is not available on this deployment.",
            )
            return

        url = _normalize(raw_arg)
        if url is None:
            await ctx.response_formatter.safe_reply(
                ctx.message,
                f"That does not look like a GitHub repository.\n\n{_USAGE}",
            )
            return

        try:
            result = await self._add_repository_factory().add(
                url=url,
                user_id=ctx.uid,
                mode="star",
                correlation_id=ctx.correlation_id,
            )
        except Exception as exc:
            # The use case raises only when nothing visible happened; anything it
            # did manage to do arrives in result.warnings instead.
            logger.warning("star_command_failed", extra=ctx.log_extra(url=url, error=str(exc)))
            await ctx.response_formatter.safe_reply(
                ctx.message,
                f"Could not star that repository: {exc}\nError ID: {ctx.correlation_id}",
            )
            return

        await ctx.response_formatter.safe_reply(ctx.message, _render(result))
        logger.info(
            "star_command_completed",
            extra=ctx.log_extra(
                full_name=result.full_name,
                lists=list(result.lists_applied),
                source=result.list_suggestion_source,
                warnings=len(result.warnings),
            ),
        )


def _normalize(raw: str) -> str | None:
    """Turn ``owner/name`` or a GitHub URL into a repository URL, or reject it."""
    candidate = raw.split(maxsplit=1)[0].strip()
    if "://" in candidate:
        return candidate if "github.com" in candidate else None
    parts = [part for part in candidate.strip("/").split("/") if part]
    if len(parts) != 2:
        return None
    return f"https://github.com/{parts[0]}/{parts[1]}"


def _render(result: Any) -> str:
    """Report what happened, including the parts that did not.

    A repository that was starred but not filed is a truthful partial success and
    is reported as one: the star is the irreversible half, so hiding the shortfall
    would leave the user believing the filing happened too.
    """
    lines = [f"Starred: {result.full_name}"]

    if result.lists_applied:
        origin = {
            "knn": "matched your existing filing",
            "llm": "chosen by model",
            "explicit": "as requested",
        }.get(result.list_suggestion_source, result.list_suggestion_source)
        lines.append(f"Filed under: {', '.join(result.lists_applied)} ({origin})")
    else:
        lines.append("Not filed: no list was a confident match. File it by hand if you want one.")

    if result.mirror_id is not None:
        lines.append("Enrolled for backup.")

    lines.extend(f"Warning: {warning}" for warning in result.warnings)
    return "\n".join(lines)
