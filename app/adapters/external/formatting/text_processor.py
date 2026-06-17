"""Text processing and chunking operations."""

from __future__ import annotations

import html
import re
import unicodedata
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.adapters.external.formatting.html_repair import repair_html_chunk
from app.adapters.external.formatting.markdown_telegram import render_markdown
from app.core.logging_utils import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    from app.adapters.external.formatting.protocols import ResponseSender


class TextProcessorImpl:
    """Implementation of text processing and chunking operations."""

    def __init__(
        self,
        response_sender: ResponseSender,
        *,
        max_message_chars: int = 3900,
    ) -> None:
        self._response_sender = response_sender
        self._max_message_chars = max_message_chars

    @property
    def max_message_chars(self) -> int:
        """Per-message character ceiling (single source of truth for splitting)."""
        return self._max_message_chars

    def chunk_text(self, text: str, *, max_len: int, html_aware: bool = False) -> list[str]:
        """Split text into chunks respecting Telegram's message length limit.

        When *html_aware* is True, each chunk is repaired so that HTML tags
        broken by the split are properly closed/reopened.  This prevents
        Telegram 400 errors when sending HTML-parsed messages.
        """
        text = text.strip()
        if not text:
            return []

        chunks: list[str] = []
        remaining = text
        while len(remaining) > max_len:
            split_idx = self._find_split_index(remaining, max_len)
            chunk = remaining[:split_idx].rstrip("\n")
            if not chunk:
                chunk = remaining[:max_len]
                split_idx = max_len
            chunks.append(chunk)
            remaining = remaining[split_idx:]
            remaining = remaining.lstrip(" \n\r")
        if remaining:
            chunks.append(remaining)

        if html_aware:
            chunks = [repair_html_chunk(c) for c in chunks]

        return chunks

    def _find_split_index(self, text: str, limit: int) -> int:
        """Find a sensible split index before the limit."""
        min_split = max(20, limit // 4)
        delimiters = [
            "\n\n",
            "\n",
            ". ",
            "! ",
            "? ",
            "; ",
            ": ",
            ", ",
            " ",
        ]
        for delim in delimiters:
            idx = text.rfind(delim, 0, limit)
            if idx >= min_split:
                return min(limit, idx + len(delim))
        return limit

    def sanitize_summary_text(self, text: str) -> str:
        """Normalize and clean summary text for safe sending.

        - Normalize to NFC
        - Remove control characters
        - Drop trailing isolated CJK run (1-3 chars) that looks like a stray token
        """
        try:
            s = unicodedata.normalize("NFC", text)
        except Exception:
            logger.debug("unicode_normalization_failed", exc_info=True)
            s = text
        # Remove control and non-printable chars
        s = "".join(ch for ch in s if unicodedata.category(ch)[0] != "C")

        # If string ends with 1-3 CJK chars and preceding 15 chars have no CJK, drop the tail
        tail_match = re.search(r"([\u4E00-\u9FFF]{1,3})$", s)
        if tail_match:
            start = max(0, len(s) - 20)
            window = s[start : len(s) - len(tail_match.group(1))]
            if not re.search(r"[\u4E00-\u9FFF]", window):
                s = s[: -len(tail_match.group(1))].rstrip("-—")

        s = s.strip()

        if s and s[-1] not in ".!?…":
            last_sentence_end = max(s.rfind("."), s.rfind("!"), s.rfind("?"), s.rfind("…"))
            if last_sentence_end != -1 and last_sentence_end >= len(s) // 3:
                s = s[: last_sentence_end + 1].rstrip()
            else:
                s = s.rstrip("-—")
                if s and s[-1] not in ".!?…":
                    s = s + "."

        return s

    def slugify(self, text: str, *, max_len: int = 60) -> str:
        """Create a filesystem-friendly slug from text."""
        text = text.strip().lower()
        # Replace non-word characters with hyphens
        text = re.sub(r"[^\w\-\s]", "", text)
        text = re.sub(r"[\s_]+", "-", text)
        text = re.sub(r"-+", "-", text).strip("-")
        if len(text) > max_len:
            text = text[:max_len].rstrip("-")
        return text or "summary"

    def build_json_filename(self, obj: dict[str, Any]) -> str:
        """Build a descriptive filename for the JSON attachment."""
        # Prefer SEO keywords; fallback to first words of TL;DR
        seo = obj.get("seo_keywords") or []
        base: str | None = None
        if isinstance(seo, list) and seo:
            base = "-".join(self.slugify(str(x)) for x in seo[:3] if str(x).strip())
        if not base:
            tl = str(obj.get("summary_250", "")).strip()
            if tl:
                # Use first 6 words
                words = re.findall(r"\w+", tl)[:6]
                base = self.slugify("-".join(words))
        if not base:
            base = "summary"
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return f"{base}-{timestamp}.json"

    def markdown_to_telegram_html(self, text: str) -> str:
        """Convert Markdown to Telegram-supported HTML.

        Delegates to :func:`render_markdown`, which parses with markdown-it-py
        (CommonMark) and emits only Telegram's HTML whitelist -- bold, italic,
        underline, strike, code/pre, links, lists, and (expandable) blockquotes.
        Headings degrade to bold lines; unsupported constructs (tables, raw
        HTML) degrade to escaped text. All text content is HTML-escaped.
        """
        return render_markdown(text)

    def linkify_urls(self, text: str) -> str:
        """Convert bare URLs in text to clickable HTML links.

        Only linkifies URLs that aren't already inside href attributes.
        Long URLs are truncated for display but full URL is preserved in href.

        Args:
            text: Text that may contain bare URLs.

        Returns:
            Text with bare URLs converted to <a href="...">...</a> links.
        """
        # Pattern to match URLs not already in href="..."
        # Negative lookbehind for href=" to avoid double-linking
        url_pattern = r'(?<!href=")(?<!">)(https?://[^\s<>"\']+)'

        def replace_url(match: re.Match[str]) -> str:
            url = match.group(1)
            # Escape URL for href attribute
            escaped_url = html.escape(url, quote=True)
            # Truncate display text for long URLs
            display = url[:47] + "..." if len(url) > 50 else url
            display_escaped = html.escape(display)
            return f'<a href="{escaped_url}">{display_escaped}</a>'

        return re.sub(url_pattern, replace_url, text)

    async def send_long_text(
        self, message: Any, text: str, *, parse_mode: str | None = None
    ) -> None:
        """Send text, splitting into multiple messages if too long for Telegram."""
        is_html = isinstance(parse_mode, str) and parse_mode.upper() == "HTML"
        for chunk in self.chunk_text(text, max_len=self._max_message_chars, html_aware=is_html):
            if chunk:
                await self._response_sender.safe_reply(message, chunk, parse_mode=parse_mode)

    async def send_markdown(self, message: Any, md_text: str) -> None:
        """Render a Markdown string to Telegram HTML and send it (split if long)."""
        rendered = render_markdown(md_text)
        if rendered:
            await self.send_long_text(message, rendered, parse_mode="HTML")

    async def send_labelled_text(self, message: Any, label: str, body: str) -> None:
        """Send labelled text, splitting into continuation messages when needed."""
        body = body.strip()
        if not body:
            return
        label_clean = label.rstrip(":")
        primary_title = f"{label_clean}:"
        chunk_limit = max(200, self._max_message_chars - len(primary_title) - 20)
        chunks = self.chunk_text(body, max_len=chunk_limit)
        if not chunks:
            return

        await self._response_sender.safe_reply(message, f"{primary_title}\n{chunks[0]}")
        for idx, chunk in enumerate(chunks[1:], start=2):
            continuation_title = f"{label_clean} (cont. {idx}):"
            await self._response_sender.safe_reply(message, f"{continuation_title}\n{chunk}")
