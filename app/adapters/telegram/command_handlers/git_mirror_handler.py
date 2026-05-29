"""Telegram command handler for /mirror and /mirrors.

/mirror <url-or-owner/name>  -- register a git mirror target (queued for next sync run).
/mirrors                     -- list the user's git mirror rows with status.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from app.adapters.git_backup.repository import GitMirrorRepository
from app.adapters.telegram.command_handlers.base_handler import HandlerDependenciesMixin
from app.adapters.telegram.command_handlers.decorators import combined_handler
from app.core.git_url_safety import assert_safe_git_url
from app.core.logging_utils import get_logger
from app.db.models.git_backup import GitMirrorSource

if TYPE_CHECKING:
    from app.adapters.telegram.command_handlers.execution_context import (
        CommandExecutionContext,
    )

logger = get_logger(__name__)

# Matches "owner/name" shorthand without a scheme (e.g. "torvalds/linux").
_OWNER_REPO_RE = re.compile(r"^[\w.\-]+/[\w.\-]+$")


def _parse_mirror_arg(raw: str) -> tuple[str, str | None]:
    """Return (clone_url, display_name) from a /mirror argument.

    Accepts:
    - A full https:// or git:// or ssh:// URL (used as-is).
    - A bare "owner/name" token expanded to https://github.com/<owner>/<name>.git.
    """
    arg = raw.strip()
    if arg.startswith(("https://", "http://", "git://", "ssh://", "git@")):
        return arg, None
    if _OWNER_REPO_RE.match(arg):
        return f"https://github.com/{arg}.git", arg
    return arg, None


def _format_mirror_row(mirror: object) -> str:
    """Format one GitMirror row for display."""
    name = getattr(mirror, "name", None) or getattr(mirror, "clone_url", "?")
    status = getattr(mirror, "status", "?")
    last_mirrored = getattr(mirror, "last_mirrored_at", None)
    last_mirrored_str = last_mirrored.strftime("%Y-%m-%d %H:%M UTC") if last_mirrored else "never"
    url = getattr(mirror, "clone_url", "")
    return (
        f"[{getattr(mirror, 'id', '?')}] {name}  status={status}  last={last_mirrored_str}\n  {url}"
    )


class GitMirrorHandler(HandlerDependenciesMixin):
    """Handle /mirror and /mirrors commands."""

    @property
    def _mirror_repo(self) -> GitMirrorRepository:
        return GitMirrorRepository(self._db, self._cfg.git_backup)

    @combined_handler("command_mirror", "mirror", include_text=True)
    async def handle_mirror(self, ctx: CommandExecutionContext) -> None:
        """Handle /mirror <url-or-owner/name>.

        Registers the target in git_mirrors (PENDING) and confirms to the user.
        The actual clone/fetch happens on the next scheduled sync run so the bot
        handler returns immediately without blocking on git I/O.
        """
        raw_arg = ctx.text[len("/mirror") :].strip()

        if not raw_arg:
            await ctx.response_formatter.safe_reply(
                ctx.message,
                "Usage: /mirror <git-url>  or  /mirror owner/name\n"
                "Example: /mirror torvalds/linux\n"
                "Example: /mirror https://github.com/foo/bar.git",
            )
            return

        clone_url, display_name = _parse_mirror_arg(raw_arg)

        # SSRF guard: reject literal private/loopback/link-local hosts up front
        # (the worker re-checks with DNS resolution before cloning).
        try:
            assert_safe_git_url(clone_url)
        except ValueError:
            await ctx.response_formatter.safe_reply(
                ctx.message,
                "That URL targets a non-public address and cannot be mirrored.",
            )
            return

        mirror = await self._mirror_repo.upsert_target(
            user_id=ctx.uid,
            source=GitMirrorSource.MANUAL,
            clone_url=clone_url,
            name=display_name,
        )

        if mirror.status.value == "pending" or getattr(mirror, "last_mirrored_at", None) is None:
            status_note = "Queued; will sync on next scheduled run."
        else:
            status_note = (
                f"Already tracked (status={mirror.status.value}). Will re-sync on next run."
            )

        label = display_name or clone_url
        await ctx.response_formatter.safe_reply(
            ctx.message,
            f"Mirror registered: {label}\n{status_note}",
        )
        logger.info(
            "git_mirror_registered",
            extra=ctx.log_extra(mirror_id=mirror.id, clone_url=clone_url),
        )

    @combined_handler("command_mirrors", "mirrors")
    async def handle_mirrors(self, ctx: CommandExecutionContext) -> None:
        """Handle /mirrors -- list the user's git mirror rows."""
        mirrors = await self._mirror_repo.list_for_user(ctx.uid)

        if not mirrors:
            await ctx.response_formatter.safe_reply(
                ctx.message,
                "No git mirrors registered yet.\nUse /mirror <url> to add one.",
            )
            return

        lines = [_format_mirror_row(m) for m in mirrors]
        text = f"Your git mirrors ({len(mirrors)}):\n\n" + "\n\n".join(lines)
        await ctx.response_formatter.safe_reply(ctx.message, text)
