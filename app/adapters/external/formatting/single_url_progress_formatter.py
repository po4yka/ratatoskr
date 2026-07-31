"""
Single-URL Progress Formatter.

Formats progress messages for single-URL processing (LLM analysis, YouTube downloads).
Similar to BatchProgressFormatter but tailored for single-URL workflows.
"""

from __future__ import annotations

import html
import time

from app.core.ui_strings import t


class SingleURLProgressFormatter:
    """Formats progress messages for single-URL operations."""

    @staticmethod
    def _format_duration(seconds: float) -> str:
        """Format duration as human-readable string.

        Examples:
            12.5 -> "12s"
            75.0 -> "1m 15s"
            3665.0 -> "1h 1m"
        """
        if seconds < 60:
            return f"{int(seconds)}s"
        if seconds < 3600:
            mins = int(seconds // 60)
            secs = int(seconds % 60)
            return f"{mins}m {secs}s" if secs > 0 else f"{mins}m"
        hours = int(seconds // 3600)
        mins = int((seconds % 3600) // 60)
        return f"{hours}h {mins}m" if mins > 0 else f"{hours}h"

    @staticmethod
    def _get_spinner() -> str:
        """Get animated spinner character based on time.

        Returns a different frame every ~0.5s for smooth animation.
        """
        frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        idx = int(time.time() * 2) % len(frames)
        return frames[idx]

    @classmethod
    def format_youtube_progress(
        cls,
        video_id: str,
        stage: int,
        stage_name: str,
        stage_elapsed_sec: float,
        completed_stages: list[tuple[str, float]],
        total_elapsed_sec: float,
        lang: str = "en",
    ) -> str:
        """Format YouTube download progress message (HTML).

        Args:
            video_id: YouTube video ID
            stage: Current stage number (1, 2, or 3)
            stage_name: Current stage description
            stage_elapsed_sec: Elapsed time for current stage
            completed_stages: List of (stage_name, duration) for completed stages
            total_elapsed_sec: Total elapsed time across all stages
            lang: UI language code ("en" or "ru").

        Returns:
            HTML-formatted progress message
        """
        spinner = cls._get_spinner()
        stage_duration = cls._format_duration(stage_elapsed_sec)
        total_duration = cls._format_duration(total_elapsed_sec)

        # Build stage status lines
        stage_lines = []
        for idx, (name, duration) in enumerate(completed_stages, start=1):
            dur_str = cls._format_duration(duration)
            stage_lines.append(f"Stage {idx}/3: ✅ {name} ({dur_str})")

        # Current stage
        stage_lines.append(f"Stage {stage}/3: 📥 {stage_name} ({stage_duration}) {spinner}")

        stages_text = "\n".join(stage_lines)

        return (
            f"\U0001f3a5 <b>{t('progress_youtube_processing', lang)}</b>\n\n"
            f"{stages_text}\n"
            f"Video ID: <code>{html.escape(video_id)}</code>\n"
            f"Quality: 1080p\n\n"
            f"<b>{t('progress_total', lang)}:</b> {total_duration}"
        )

    @classmethod
    def format_youtube_complete(
        cls,
        title: str,
        size_mb: float,
        total_elapsed_sec: float,
        success: bool = True,
        error_msg: str | None = None,
        correlation_id: str | None = None,
        failed_stage: str | None = None,
        lang: str = "en",
    ) -> str:
        """Format YouTube completion message (HTML).

        Args:
            title: Video title
            size_mb: File size in megabytes
            total_elapsed_sec: Total elapsed time
            success: Whether processing succeeded
            error_msg: Error message (if failed)
            correlation_id: Correlation ID for error tracking
            failed_stage: Stage description where failure occurred
            lang: UI language code ("en" or "ru").

        Returns:
            HTML-formatted completion message
        """
        duration = cls._format_duration(total_elapsed_sec)

        if success:
            display_title = title[:100] + "..." if len(title) > 100 else title
            return (
                f"\u2705 <b>{t('progress_video_complete', lang)}</b> ({duration})\n\n"
                f"\U0001f4f9 Title: {html.escape(display_title)}\n"
                f"\U0001f4be Size: {size_mb:.1f} MB\n"
                f"\U0001f4dd {t('progress_transcript_ready', lang)}"
            )
        error_text = html.escape(error_msg or "Unknown error")
        error_id_line = (
            f"\n{t('progress_error_id', lang)}: <code>{correlation_id}</code>"
            if correlation_id
            else ""
        )
        stage_line = f"{failed_stage}\n" if failed_stage else ""
        return (
            f"\u274c <b>{t('progress_video_failed', lang)}</b> ({duration})\n\n"
            f"{stage_line}"
            f"{t('progress_error', lang)}: {error_text}{error_id_line}"
        )
