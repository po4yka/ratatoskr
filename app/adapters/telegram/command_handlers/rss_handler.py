"""RSS feed management command handler (/rss).

Lets users manage RSS feed subscriptions via Telegram commands.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.adapters.telegram.command_handlers.base_handler import HandlerDependenciesMixin
from app.adapters.telegram.command_handlers.decorators import combined_handler
from app.core.logging_utils import get_logger
from app.domain.services.import_export.opml_exporter import OPMLExporter

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.adapters.telegram.command_handlers.execution_context import (
        CommandExecutionContext,
    )

logger = get_logger(__name__)

_MAX_ITEMS_PREVIEW = 5


class RSSHandler(HandlerDependenciesMixin):
    """Handle /rss commands for RSS feed management."""

    def __init__(
        self,
        cfg: Any,
        db: Any,
        response_formatter: Any,
        *,
        rss_repo_factory: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__(cfg, db, response_formatter)
        self._rss_repo_factory = rss_repo_factory

    @property
    def _rss_repo(self) -> Any:
        if self._rss_repo_factory is None:
            msg = "RSS repository factory is not configured"
            raise RuntimeError(msg)
        return self._rss_repo_factory()

    @combined_handler("command_substack", "substack")
    async def handle_substack(self, ctx: CommandExecutionContext) -> None:
        """Handle /substack [subcommand].

        Subcommands:
            <name>        -- subscribe to a Substack publication by name or URL
            add <name>    -- same as above
            list          -- list Substack subscriptions
            remove <id>   -- unsubscribe by subscription ID
        """
        args = ctx.text[len("/substack") :].strip()

        from app.core.substack_utils import resolve_substack_feed_url

        if args == "list":
            await self._handle_substack_list(ctx)
        elif args.startswith("remove "):
            await self._handle_remove(ctx, args[7:].strip())
        elif args.startswith("add "):
            feed_url = resolve_substack_feed_url(args[4:].strip())
            await self._handle_add(ctx, feed_url)
        elif args:
            feed_url = resolve_substack_feed_url(args)
            await self._handle_add(ctx, feed_url)
        else:
            await ctx.response_formatter.safe_reply(
                ctx.message,
                "Usage:\n"
                "  /substack <name> -- subscribe (e.g. /substack platformer)\n"
                "  /substack list -- list Substack subscriptions\n"
                "  /substack remove <id> -- unsubscribe",
            )

    async def _handle_substack_list(self, ctx: CommandExecutionContext) -> None:
        """List only Substack RSS subscriptions."""
        subs = await self._rss_repo.async_list_user_active_subscriptions(
            ctx.uid,
            substack_only=True,
        )

        lines: list[str] = []
        for sub in subs:
            feed = sub.get("feed") if isinstance(sub.get("feed"), dict) else {}
            title = feed.get("title") or feed.get("url")
            lines.append(f"[{sub.get('id')}] {title}\n  {feed.get('url')}")

        if not lines:
            await ctx.response_formatter.safe_reply(
                ctx.message,
                "No Substack subscriptions yet.\n"
                "Use /substack <name> to subscribe (e.g. /substack platformer).",
            )
            return

        text = "Your Substack subscriptions:\n\n" + "\n\n".join(lines)
        await ctx.response_formatter.safe_reply(ctx.message, text)

    @combined_handler("command_rss", "rss")
    async def handle_rss(self, ctx: CommandExecutionContext) -> None:
        """Handle /rss [subcommand].

        Subcommands:
            (none)        -- list subscriptions
            add <url>     -- subscribe to a feed
            remove <id>   -- unsubscribe by subscription ID
            export        -- generate and send OPML file
        """
        args = ctx.text[len("/rss") :].strip()

        if args.startswith("add "):
            await self._handle_add(ctx, args[4:].strip())
        elif args.startswith("remove "):
            await self._handle_remove(ctx, args[7:].strip())
        elif args == "export":
            await self._handle_export(ctx)
        else:
            await self._handle_list(ctx)

    async def _handle_list(self, ctx: CommandExecutionContext) -> None:
        """List all RSS subscriptions for the user."""
        subs = await self._rss_repo.async_list_user_active_subscriptions(ctx.uid)

        lines: list[str] = []
        for sub in subs:
            feed = sub.get("feed") if isinstance(sub.get("feed"), dict) else {}
            status = "active" if feed.get("is_active") else "paused"
            title = feed.get("title") or feed.get("url")
            errors = int(feed.get("fetch_error_count") or 0)
            errors_part = f" ({errors} errors)" if errors else ""
            lines.append(f"[{sub.get('id')}] {title}\n  {feed.get('url')} [{status}]{errors_part}")

        if not lines:
            await ctx.response_formatter.safe_reply(
                ctx.message,
                "No RSS subscriptions yet.\nUse /rss add <url> to subscribe to a feed.",
            )
            return

        text = "Your RSS subscriptions:\n\n" + "\n\n".join(lines)
        await ctx.response_formatter.safe_reply(ctx.message, text)

    async def _handle_add(self, ctx: CommandExecutionContext, url: str) -> None:
        """Subscribe to an RSS feed by URL."""
        if not url:
            await ctx.response_formatter.safe_reply(
                ctx.message,
                "Usage: /rss add <feed_url>",
            )
            return

        from app.core.substack_utils import is_substack_url, resolve_substack_feed_url

        # Normalize URL
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        # Auto-resolve Substack URLs to their feed endpoint
        if is_substack_url(url) and not url.rstrip("/").endswith("/feed"):
            url = resolve_substack_feed_url(url)

        # Find or create the feed
        feed = await self._rss_repo.async_get_or_create_feed(url)

        # Check if already subscribed
        existing = await self._rss_repo.async_get_subscription_by_feed(
            user_id=ctx.uid,
            feed_id=int(feed["id"]),
        )
        if existing:
            if not existing.get("is_active"):
                await self._rss_repo.async_set_subscription_active(
                    int(existing["id"]), is_active=True, user_id=ctx.uid
                )
                await ctx.response_formatter.safe_reply(
                    ctx.message,
                    f"Re-activated subscription to {feed.get('title') or feed.get('url')}",
                )
                return
            await ctx.response_formatter.safe_reply(
                ctx.message,
                f"Already subscribed to {feed.get('title') or feed.get('url')}",
            )
            return

        await self._rss_repo.async_create_subscription(
            user_id=ctx.uid,
            feed_id=int(feed["id"]),
        )

        title_display = feed.get("title") or url
        await ctx.response_formatter.safe_reply(
            ctx.message,
            f"Subscribed to {title_display}\nThe feed will be fetched on the next polling cycle.",
        )

    async def _handle_remove(self, ctx: CommandExecutionContext, sub_id_str: str) -> None:
        """Unsubscribe from a feed by subscription ID."""
        try:
            sub_id = int(sub_id_str)
        except ValueError:
            await ctx.response_formatter.safe_reply(
                ctx.message,
                "Usage: /rss remove <subscription_id>\nUse /rss to see your subscription IDs.",
            )
            return

        sub = await self._rss_repo.async_get_subscription(user_id=ctx.uid, subscription_id=sub_id)
        if sub is None:
            await ctx.response_formatter.safe_reply(
                ctx.message,
                f"Subscription #{sub_id} not found.",
            )
            return

        feed = sub.get("feed") if isinstance(sub.get("feed"), dict) else {}
        feed_title = feed.get("title") or feed.get("url")
        await self._rss_repo.async_set_subscription_active(sub_id, is_active=False, user_id=ctx.uid)

        await ctx.response_formatter.safe_reply(
            ctx.message,
            f"Unsubscribed from {feed_title}",
        )

    async def _handle_export(self, ctx: CommandExecutionContext) -> None:
        """Export subscriptions as OPML and send as a document."""
        subs = await self._rss_repo.async_list_user_active_subscriptions(ctx.uid)

        feeds_data: list[dict] = []
        for sub in subs:
            feed = sub.get("feed") if isinstance(sub.get("feed"), dict) else {}
            feeds_data.append(
                {
                    "url": feed.get("url"),
                    "title": feed.get("title"),
                    "site_url": feed.get("site_url"),
                    "category_name": sub.get("category_name"),
                }
            )

        if not feeds_data:
            await ctx.response_formatter.safe_reply(
                ctx.message,
                "No active subscriptions to export.",
            )
            return

        exporter = OPMLExporter()
        opml_xml = exporter.serialize(feeds_data)

        import tempfile
        from pathlib import Path

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".opml", delete=False, encoding="utf-8"
        ) as f:
            f.write(opml_xml)
            tmp_path = Path(f.name)

        try:
            await ctx.message.reply_document(
                document=str(tmp_path),
                file_name="rss_feeds.opml",
                caption=f"Exported {len(feeds_data)} RSS feed subscriptions.",
            )
        finally:
            tmp_path.unlink(missing_ok=True)
