"""Formatter for batch URL processing progress and completion messages.

Provides rich, informative messages showing per-URL status with numbered lines,
ETA estimates, and detailed completion reports with titles and error reasons.
"""

from __future__ import annotations

import html
import time
from typing import TYPE_CHECKING

from app.adapter_models.batch_processing import URLStatus
from app.adapters.external.formatting.error_types import ErrorNotificationType

if TYPE_CHECKING:
    from app.adapter_models.batch_processing import URLBatchStatus, URLStatusEntry


# Telegram message limit with safety margin
MAX_MESSAGE_LENGTH = 3500


class BatchProgressFormatter:
    """Formats batch processing progress and completion messages.

    All output uses Telegram HTML parse mode. Callers must pass
    ``parse_mode="HTML"`` when sending/editing these messages.

    Provides multiple display formats:
    - Detailed: Numbered per-URL status lines with elapsed time
    - Compact: Summarized counts for long completion messages
    - Minimal: Simple counts for very long batches
    """

    # ------------------------------------------------------------------
    # HTML helpers
    # ------------------------------------------------------------------

    @classmethod
    def _make_link(cls, url: str, display_text: str) -> str:
        """Create an HTML ``<a>`` hyperlink safe for Telegram."""
        return f'<a href="{html.escape(url)}">{html.escape(display_text)}</a>'

    @classmethod
    def _get_spinner(cls, tick: int | None = None) -> str:
        """Get an 'animated' spinner based on elapsed seconds tick.

        Args:
            tick: Integer tick (typically elapsed seconds). If None, uses current time.
        """
        frames = [
            "\u280b",
            "\u2819",
            "\u2839",
            "\u2838",
            "\u283c",
            "\u2834",
            "\u2826",
            "\u2827",
            "\u2807",
            "\u280f",
        ]
        t = tick if tick is not None else int(time.time())
        return frames[t % len(frames)]

    @classmethod
    def _format_progress_bar(cls, done: int, total: int, width: int = 10) -> str:
        """Format a text progress bar.

        Example: ``[======----] 3/5 (60%)``
        """
        if total <= 0:
            return f"[{'-' * width}] 0/0 (0%)"
        filled = round(done / total * width)
        bar = "=" * filled + "-" * (width - filled)
        percentage = int(done / total * 100)
        return f"[{bar}] {done}/{total} ({percentage}%)"

    @classmethod
    def format_progress_message(cls, batch: URLBatchStatus) -> str:
        """Format a progress update message with numbered per-URL status lines.

        Example output::

            Processing 4 links... (45s elapsed)

            [1/4] techcrunch.com/article  Extracting... (5s)
            [2/4] arxiv.org/.../2401.1234  Analyzing [deepseek-v3]... (12s)
            [Cache] medium.com/.../slug  Done (cached)
            [4/4] github.io/page  Pending

            [======----] 2/4 (50%) | ETA: ~30s | Elapsed: 45s

        Args:
            batch: Current batch status

        Returns:
            Formatted progress message string
        """
        lines: list[str] = []
        total = batch.total
        elapsed_sec = batch.total_elapsed_time_sec()
        elapsed_str = cls._format_duration(elapsed_sec)
        tick = int(elapsed_sec)
        spinner = cls._get_spinner(tick)

        # Header
        lines.append(f"<b>Processing {total} links...</b> ({elapsed_str} elapsed) {spinner}")
        lines.append("")

        # Per-URL status lines
        for index, entry in enumerate(batch.entries, 1):
            lines.append(cls._format_status_line(index, total, entry, tick=tick))

        lines.append("")

        # Footer: progress bar + ETA + elapsed
        done = batch.done_count
        footer_parts = [cls._format_progress_bar(done, total)]

        eta_sec = batch.estimate_remaining_time_sec()
        if eta_sec is not None and eta_sec > 0:
            footer_parts.append(f"ETA: ~{cls._format_duration(eta_sec)}")

        footer_parts.append(f"Elapsed: {elapsed_str}")
        lines.append(" | ".join(footer_parts))

        message = "\n".join(lines)

        # Fallback to compact then minimal if too long
        if len(message) > MAX_MESSAGE_LENGTH:
            compact = cls._format_compact_progress(batch)
            if len(compact) <= MAX_MESSAGE_LENGTH:
                return compact
            return cls._format_minimal_progress(batch)

        return message

    @classmethod
    def _format_status_line(
        cls,
        index: int,
        total: int,
        entry: URLStatusEntry,
        *,
        tick: int | None = None,
    ) -> str:
        """Format a single numbered status line for progress display (HTML).

        The domain label is rendered as a clickable hyperlink to the original URL.
        Cached items use "[Cache]" prefix instead of numbers.

        Args:
            index: 1-based index of the entry
            total: Total number of entries in the batch
            entry: The URL status entry to format
            tick: Elapsed-seconds tick for stable spinner animation

        Returns:
            HTML-formatted status line, e.g.
            ``[2/5] <a href="...">arxiv.org</a>  Analyzing... (3s)``
        """
        is_cached = entry.status == URLStatus.CACHED
        prefix = "<code>[Cache]</code>" if is_cached else f"<code>[{index}/{total}]</code>"

        label = entry.display_label or entry.domain or "unknown"
        link = cls._make_link(entry.url, label)
        spinner = cls._get_spinner(tick)

        if entry.status == URLStatus.COMPLETE:
            elapsed = cls._format_elapsed(entry.processing_time_ms)
            return f"{prefix} {link}  Done{elapsed}"

        if entry.status == URLStatus.CACHED:
            return f"{prefix} {link}  Done (cached)"

        if entry.status == URLStatus.FAILED:
            error = cls._format_error_short(entry.error_type, entry.error_message)
            elapsed = cls._format_elapsed(entry.processing_time_ms)
            return f"{prefix} {link}  Failed: {html.escape(error)}{elapsed}"

        if entry.status == URLStatus.EXTRACTING:
            live = cls._format_live_elapsed(entry.start_time)
            return f"{prefix} {link}  Extracting...{live} {spinner}"

        if entry.status == URLStatus.ANALYZING:
            live = cls._format_live_elapsed(entry.start_time)
            label = entry.title or label
            link = cls._make_link(entry.url, label)
            detail = ""
            if entry.model:
                m = entry.model.split("/")[-1]
                detail = f" [{m}]"
            return f"{prefix} {link}  Analyzing{detail}...{live} {spinner}"

        if entry.status == URLStatus.SUMMARIZING:
            live = cls._format_live_elapsed(entry.start_time)
            label = entry.title or label
            link = cls._make_link(entry.url, label)
            detail = ""
            if entry.model:
                m = entry.model.split("/")[-1]
                detail = f" [{m}]"
            return f"{prefix} {link}  Summarizing{detail}...{live} {spinner}"

        if entry.status == URLStatus.RETRYING:
            live = cls._format_live_elapsed(entry.start_time)
            retry_info = ""
            if entry.max_retries > 0:
                retry_info = f" ({entry.retry_count}/{entry.max_retries})"
            return f"{prefix} {link}  Retrying{retry_info}...{live} {spinner}"

        if entry.status == URLStatus.RETRY_WAITING:
            retry_info = ""
            if entry.max_retries > 0:
                retry_info = f" ({entry.retry_count}/{entry.max_retries})"
            return f"{prefix} {link}  Waiting to retry{retry_info}... {spinner}"

        if entry.status == URLStatus.PROCESSING:
            live = cls._format_live_elapsed(entry.start_time)
            return f"{prefix} {link}  Processing...{live} {spinner}"

        # PENDING (default)
        return f"{prefix} {link}  Pending"

    @classmethod
    def _format_elapsed(cls, processing_time_ms: float) -> str:
        """Format completed processing time as a parenthesized suffix.

        Args:
            processing_time_ms: Processing time in milliseconds

        Returns:
            ``" (12s)"`` if time is positive, otherwise ``""``
        """
        if processing_time_ms > 0:
            seconds = processing_time_ms / 1000
            return f" ({cls._format_duration(seconds)})"
        return ""

    @classmethod
    def _format_live_elapsed(cls, start_time: float | None) -> str:
        """Format live elapsed time since processing started.

        Args:
            start_time: Unix timestamp when processing started, or None

        Returns:
            ``" (5s)"`` if elapsed >= 1 second, otherwise ``""``
        """
        if start_time is None:
            return ""
        elapsed = time.time() - start_time
        if elapsed >= 1:
            return f" ({cls._format_duration(elapsed)})"
        return ""

    @classmethod
    def format_completion_message(cls, batch: URLBatchStatus) -> str:
        """Format a completion message with a unified numbered list.

        Example output::

            Batch Complete -- 3/4 links

            1. "AI advances in 2026" -- techcrunch.com (12s)
            2. "Attention Is All You Need" -- arxiv.org (18s)
            3. "Rust vs Go" -- medium.com (15s)
            4. github.io -- Failed: Timeout (90s)

            Total: 1m 32s | Avg: 15s/link

        Args:
            batch: Completed batch status

        Returns:
            Formatted completion message string
        """
        lines: list[str] = []

        total = batch.total
        success = batch.success_count

        # Header
        total_time = batch.total_elapsed_time_sec()
        duration_str = cls._format_duration(total_time)
        lines.append(f"<b>Batch Complete</b>  {success}/{total} links ({duration_str})")
        lines.append("")

        # Unified numbered list (HTML)
        for index, entry in enumerate(batch.entries, 1):
            lines.append(cls._format_completion_line(index, entry))

        lines.append("")

        # Timing footer
        total_time = batch.total_elapsed_time_sec()
        avg_ms = batch.average_processing_time_ms()
        avg_str = cls._format_duration(avg_ms / 1000) if avg_ms > 0 else "N/A"
        lines.append(f"Total: {cls._format_duration(total_time)} | Avg: {avg_str}/link")

        message = "\n".join(lines)

        # Fallback to compact if too long
        if len(message) > MAX_MESSAGE_LENGTH:
            return cls._format_compact_completion(batch)

        return message

    @classmethod
    def _format_completion_line(cls, index: int, entry: URLStatusEntry) -> str:
        """Format a single numbered line for the completion message (HTML).

        Successful entries use the article title as clickable link text.
        Failed entries use the domain/slug label as a clickable link.

        Args:
            index: 1-based index of the entry
            entry: The URL status entry to format

        Returns:
            HTML-formatted completion line, e.g.
            ``1. <a href="...">Article Title</a> (12s)``
        """
        label = entry.display_label or entry.domain or "unknown"
        elapsed = cls._format_elapsed(entry.processing_time_ms)

        if entry.status in {URLStatus.COMPLETE, URLStatus.CACHED}:
            title = entry.title or "Untitled"
            if len(title) > 50:
                title = title[:47] + "..."
            link = cls._make_link(entry.url, title)
            if entry.status == URLStatus.CACHED:
                suffix = " (cached)"
            else:
                size_part = ""
                if entry.content_length and entry.content_length > 0:
                    size_part = f", {cls._format_content_size(entry.content_length)}"
                suffix = f"{elapsed}{size_part}" if (elapsed or size_part) else ""
            return f"{index}. {link}{suffix}"

        if entry.status == URLStatus.FAILED:
            error = cls._format_error_short(entry.error_type, entry.error_message)
            link = cls._make_link(entry.url, label)
            return f"{index}. {link}  Failed: {html.escape(error)}{elapsed}"

        # Shouldn't happen in a completed batch, but handle gracefully
        return f"{index}. {html.escape(label)}  {html.escape(entry.status.value)}"

    @classmethod
    def _format_compact_progress(cls, batch: URLBatchStatus) -> str:
        """Format a mid-tier compact progress message for medium-large batches.

        Shows progress bar, status category counts, failed URLs, and ETA
        without individual per-URL status lines.
        """
        lines: list[str] = []
        total = batch.total
        elapsed_sec = batch.total_elapsed_time_sec()
        elapsed_str = cls._format_duration(elapsed_sec)
        spinner = cls._get_spinner(int(elapsed_sec))

        lines.append(f"<b>Processing {total} links...</b> ({elapsed_str} elapsed) {spinner}")
        lines.append("")

        # Status category counts
        counts: list[str] = []
        done = batch.done_count
        extracting = sum(1 for e in batch.entries if e.status == URLStatus.EXTRACTING)
        analyzing = sum(1 for e in batch.entries if e.status == URLStatus.ANALYZING)
        summarizing = sum(1 for e in batch.entries if e.status == URLStatus.SUMMARIZING)
        retrying = sum(
            1 for e in batch.entries if e.status in {URLStatus.RETRYING, URLStatus.RETRY_WAITING}
        )
        pending = batch.pending_count

        if done > 0:
            counts.append(f"<b>{done}</b> done")
        if extracting > 0:
            counts.append(f"{extracting} extracting")
        if analyzing > 0:
            counts.append(f"{analyzing} analyzing")
        if summarizing > 0:
            counts.append(f"{summarizing} summarizing")
        if retrying > 0:
            counts.append(f"{retrying} retrying")
        if pending > 0:
            counts.append(f"{pending} pending")
        if counts:
            lines.append(", ".join(counts))

        # Show failed URLs with errors
        failed_entries = batch.failed
        if failed_entries:
            lines.append("")
            for entry in failed_entries[:5]:
                label = entry.display_label or entry.domain or entry.url[:20]
                error = cls._format_error_short(entry.error_type, entry.error_message)
                link = cls._make_link(entry.url, label)
                lines.append(f"  {link}: {html.escape(error)}")
            if len(failed_entries) > 5:
                lines.append(f"  ... and {len(failed_entries) - 5} more failed")

        lines.append("")

        # Footer
        footer_parts = [cls._format_progress_bar(done, total)]
        eta_sec = batch.estimate_remaining_time_sec()
        if eta_sec is not None and eta_sec > 0:
            footer_parts.append(f"ETA: ~{cls._format_duration(eta_sec)}")
        footer_parts.append(f"Elapsed: {elapsed_str}")
        lines.append(" | ".join(footer_parts))

        return "\n".join(lines)

    @classmethod
    def _format_minimal_progress(cls, batch: URLBatchStatus) -> str:
        """Format minimal progress message for very long batches."""
        done = batch.done_count
        total = batch.total
        percentage = int((done / total) * 100) if total > 0 else 0

        eta_sec = batch.estimate_remaining_time_sec()
        eta_part = f" | ETA: ~{cls._format_duration(eta_sec)}" if eta_sec else ""

        return f"Processing: {done}/{total} ({percentage}%){eta_part}"

    @classmethod
    def _format_compact_completion(cls, batch: URLBatchStatus) -> str:
        """Format compact completion message when detailed is too long (HTML)."""
        total = batch.total
        success = batch.success_count
        failed = batch.fail_count

        lines: list[str] = []

        # Header
        if failed == 0:
            lines.append(f"Batch Complete  All {success} links processed successfully!")
        elif success == 0:
            lines.append(f"Batch Failed  {failed} links failed")
        else:
            lines.append(f"Batch Complete  {success}/{total} links")
            lines.append(f"({failed} failed)")

        # Just show failed domains if any
        failed_entries = batch.failed
        if failed_entries:
            lines.append("")
            lines.append("Failed:")
            for entry in failed_entries[:3]:
                label = entry.display_label or entry.domain or entry.url[:20]
                error = cls._format_error_short(entry.error_type, entry.error_message)
                link = cls._make_link(entry.url, label)
                lines.append(f"  - {link}: {html.escape(error)}")
            if len(failed_entries) > 3:
                lines.append(f"  ... and {len(failed_entries) - 3} more")

        # Timing
        total_time = batch.total_elapsed_time_sec()
        lines.append("")
        lines.append(f"Total: {cls._format_duration(total_time)}")

        return "\n".join(lines)

    @classmethod
    def _format_error_short(cls, error_type: str | None, error_message: str | None) -> str:
        """Format error for compact display."""
        if not error_type and not error_message:
            return "Unknown error"

        e_type = str(error_type).lower() if error_type else ""
        e_msg = str(error_message).lower() if error_message else ""

        if e_type == ErrorNotificationType.TIMEOUT:
            if "after" in e_msg:
                return f"Timed out ({e_msg.split('after ')[-1]})"
            return "Timed out"

        if e_type == ErrorNotificationType.DOMAIN_TIMEOUT:
            return "Skipped (slow site)"

        if e_type == ErrorNotificationType.NETWORK_ERROR:
            if "403" in e_msg:
                return "Access Denied (403)"
            if "404" in e_msg:
                return "Not Found (404)"
            return "Network error"

        if e_type == ErrorNotificationType.VALIDATION:
            return "Invalid URL"

        if e_type in {ErrorNotificationType.RATE_LIMIT, "429"}:
            return "Rate limited"

        if "refused" in e_msg:
            return "Connection refused"

        if "cloudflare" in e_msg:
            return "Blocked by Cloudflare"

        if error_message:
            # Truncate long error messages
            msg = str(error_message)
            if len(msg) > 30:
                return msg[:27] + "..."
            return msg

        return error_type or "Error"

    @classmethod
    def _format_content_size(cls, chars: int) -> str:
        """Format content size in human-readable form (e.g., ``15k chars``)."""
        if chars >= 1000:
            return f"{chars // 1000}k chars"
        return f"{chars} chars"

    @classmethod
    def _format_duration(cls, seconds: float) -> str:
        """Format duration in human-readable form."""
        if seconds < 1:
            return "<1s"
        if seconds < 60:
            return f"{int(seconds)}s"
        if seconds < 3600:
            minutes = int(seconds // 60)
            secs = int(seconds % 60)
            if secs > 0:
                return f"{minutes}m {secs}s"
            return f"{minutes}m"
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        return f"{hours}h {minutes}m"

    @classmethod
    def get_current_processing_domain(cls, batch: URLBatchStatus) -> str | None:
        """Get the domain of the currently processing URL.

        Args:
            batch: Current batch status

        Returns:
            Domain string or None if nothing is processing
        """
        processing = batch.processing
        if processing:
            return processing[0].domain
        return None
