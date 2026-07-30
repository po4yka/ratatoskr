"""Digest formatter -- builds Telegram messages with inline buttons."""

from __future__ import annotations

from statistics import mean
from typing import Any

from app.core.logging_utils import get_logger

logger = get_logger(__name__)

# Telegram hard message length limit.
# Digest messages are pre-formatted in bulk and use the full Telegram limit (4096).
# ResponseFormatter uses 3500 (TelegramLimitsConfig.max_message_chars) as a safety
# margin for interactive replies. The difference is intentional.
MAX_MESSAGE_LENGTH = 4096

CONTENT_TYPE_EMOJI: dict[str, str] = {
    "news": "\U0001f4f0",  # newspaper
    "tutorial": "\U0001f4d6",  # open book
    "opinion": "\U0001f4ac",  # speech balloon
    "other": "\U0001f4cc",  # pushpin
}


class DigestFormatter:
    """Formats analyzed posts into Telegram digest messages with inline buttons."""

    @staticmethod
    def format_digest(
        analyzed_posts: list[dict[str, Any]],
    ) -> list[tuple[str, list[list[dict[str, str]]]]]:
        """Format analyzed posts into per-channel Telegram messages.

        Returns a header/TOC message followed by one message group per channel.
        Each channel block is self-contained with its posts and inline buttons.
        If a single channel's content exceeds MAX_MESSAGE_LENGTH chars, it is split into
        multiple messages.

        Args:
            analyzed_posts: List of post dicts with analysis fields.

        Returns:
            List of (message_text, inline_keyboard_rows) tuples.
        """
        if not analyzed_posts:
            return [
                (
                    "\u041d\u0435\u0442 \u043d\u043e\u0432\u044b\u0445 \u043f\u043e\u0441\u0442\u043e\u0432 \u0434\u043b\u044f \u0434\u0430\u0439\u0434\u0436\u0435\u0441\u0442\u0430.",
                    [],
                )
            ]

        # Group by channel
        by_channel: dict[str, list[dict[str, Any]]] = {}
        for post in analyzed_posts:
            channel = post.get("_channel_username", "unknown")
            by_channel.setdefault(channel, []).append(post)

        # Sort each group by relevance desc
        for posts_list in by_channel.values():
            posts_list.sort(key=lambda p: p.get("relevance_score", 0), reverse=True)

        # Sort channels by average relevance desc, then alphabetically
        sorted_channels = sorted(
            by_channel.keys(),
            key=lambda ch: (
                -mean(p.get("relevance_score", 0) for p in by_channel[ch]),
                ch.lower(),
            ),
        )

        # --- Build header / TOC message ---
        total_posts = sum(len(v) for v in by_channel.values())
        total_channels = len(by_channel)
        ch_word = _pluralize_channels(total_channels)
        post_word = _pluralize_posts(total_posts)
        parts: list[str] = [
            (
                f"\U0001f4cb **\u0414\u0430\u0439\u0434\u0436\u0435\u0441\u0442 \u043a\u0430\u043d\u0430\u043b\u043e\u0432** \u2014 "
                f"{total_posts} {post_word} \u0438\u0437 {total_channels} {ch_word}\n\n"
            ),
        ]
        for ch in sorted_channels:
            count = len(by_channel[ch])
            parts.append(f"  @{ch} \u2014 {count} {_pluralize_posts(count)}\n")

        header_text = "".join(parts)
        result: list[tuple[str, list[list[dict[str, str]]]]] = [(header_text, [])]

        # --- Build per-channel messages ---
        global_post_num = 0
        for channel in sorted_channels:
            channel_entries: list[tuple[str, dict[str, str]]] = []
            channel_header = f"\U0001f4e2 **@{channel}**\n\n"
            channel_entries.append((channel_header, {}))

            for post in by_channel[channel]:
                global_post_num += 1
                content_type = post.get("content_type", "other")
                emoji = CONTENT_TYPE_EMOJI.get(content_type, "\U0001f4cc")

                real_topic = post.get("real_topic", "\u0411\u0435\u0437 \u0442\u0435\u043c\u044b")
                tldr = post.get("tldr", "")
                url = post.get("url", "")

                lines = f"{global_post_num}. {emoji} **{real_topic}**\n    {tldr}\n"
                if url:
                    lines += f"    [\u0427\u0438\u0442\u0430\u0442\u044c]({url})\n"

                # Append key_insights as indented bullets
                key_insights: list[str] = post.get("key_insights") or []
                for insight in key_insights[:3]:
                    lines += f"    - {insight}\n"

                channel_id = post.get("_channel_id", 0)
                message_id = post.get("message_id", 0)
                button = {
                    "text": f"{global_post_num}. {real_topic[:30]}",
                    "callback_data": f"dg:{channel_id}:{message_id}",
                }
                channel_entries.append((lines, button))

            # Split this channel's entries into message chunks
            result.extend(_split_channel_entries(channel_entries))

        return result


def _split_channel_entries(
    entries: list[tuple[str, dict[str, str]]],
) -> list[tuple[str, list[list[dict[str, str]]]]]:
    """Build message chunks for a single channel, attaching buttons per chunk."""
    result: list[tuple[str, list[list[dict[str, str]]]]] = []
    current_text = ""
    current_buttons: list[list[dict[str, str]]] = []

    for entry_text, button in entries:
        candidate = current_text + entry_text
        if len(candidate) > MAX_MESSAGE_LENGTH and current_text.strip():
            result.append((current_text, current_buttons))
            current_text = entry_text
            current_buttons = []
        else:
            current_text = candidate
        if button:
            current_buttons.append([button])

    if current_text.strip():
        result.append((current_text, current_buttons))

    return result


def _pluralize_posts(n: int) -> str:
    """Russian pluralization for 'post'."""
    mod10 = n % 10
    mod100 = n % 100
    if mod10 == 1 and mod100 != 11:
        return "\u043f\u043e\u0441\u0442"
    if 2 <= mod10 <= 4 and not (12 <= mod100 <= 14):
        return "\u043f\u043e\u0441\u0442\u0430"
    return "\u043f\u043e\u0441\u0442\u043e\u0432"


def _pluralize_channels(n: int) -> str:
    """Russian pluralization for 'channel'."""
    mod10 = n % 10
    mod100 = n % 100
    if mod10 == 1 and mod100 != 11:
        return "\u043a\u0430\u043d\u0430\u043b"
    if 2 <= mod10 <= 4 and not (12 <= mod100 <= 14):
        return "\u043a\u0430\u043d\u0430\u043b\u0430"
    return "\u043a\u0430\u043d\u0430\u043b\u043e\u0432"
