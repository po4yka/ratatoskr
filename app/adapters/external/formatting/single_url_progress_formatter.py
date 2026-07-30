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

    @staticmethod
    def _short_model(model: str) -> str:
        """Strip provider prefix from model name (e.g. 'anthropic/claude-sonnet-4.6' -> 'claude-sonnet-4.6')."""
        return model.split("/", 1)[-1] if "/" in model else model

    @staticmethod
    def _estimate_seconds(content_length: int) -> float:
        """Estimate LLM processing time based on content length in characters."""
        if content_length < 3000:
            return 15.0
        if content_length < 10000:
            return 25.0
        if content_length < 30000:
            return 40.0
        return 60.0

    @staticmethod
    def _progress_bar(elapsed_sec: float, estimate_sec: float = 30.0) -> str:
        """Render a text progress bar based on elapsed vs estimated time."""
        ratio = min(elapsed_sec / estimate_sec, 0.95) if estimate_sec > 0 else 0.0
        filled = int(ratio * 10)
        return "\u2593" * filled + "\u2591" * (10 - filled)

    @classmethod
    def format_extraction_progress(
        cls,
        url: str,
        elapsed_sec: float,
        lang: str = "en",
    ) -> str:
        """Format content-extraction progress message (HTML).

        Args:
            url: The URL being extracted.
            elapsed_sec: Elapsed time in seconds.
            lang: UI language code ("en" or "ru").

        Returns:
            HTML-formatted extraction progress message.
        """
        spinner = cls._get_spinner()
        duration = cls._format_duration(elapsed_sec)
        display_url = url[:60] + "..." if len(url) > 60 else url
        estimate = cls._estimate_seconds(0) + 15.0  # base extraction estimate
        bar = cls._progress_bar(elapsed_sec, estimate_sec=estimate)
        eta_str = cls._format_duration(estimate)
        return (
            f"\U0001f310 <b>{t('progress_extracting_content', lang)}</b>\n\n"
            f"\U0001f517 {html.escape(display_url)}\n"
            f"\u23f1\ufe0f {t('progress_extracting', lang)} ({duration} / ~{eta_str}) {spinner}\n"
            f"<code>{bar}</code>"
        )

    @classmethod
    def format_llm_progress(
        cls,
        content_length: int,
        model: str,
        elapsed_sec: float,
        phase: str = "analyzing",
        lang: str = "en",
        *,
        content_tier: str | None = None,
        content_lang: str | None = None,
    ) -> str:
        """Format LLM analysis progress message (HTML).

        Args:
            content_length: Number of characters in content
            model: LLM model name (e.g., "anthropic/claude-sonnet-4.6")
            elapsed_sec: Elapsed time in seconds
            phase: Current phase ("analyzing", "retrying", "enriching")
            lang: UI language code ("en" or "ru").
            content_tier: Content classification tier (e.g., "technical", "sociopolitical")
            content_lang: Detected content language (e.g., "en", "ru")

        Returns:
            HTML-formatted progress message
        """
        phase_labels = {
            "analyzing": t("progress_analyzing", lang),
            "retrying": t("progress_retrying", lang),
            "enriching": t("progress_enriching", lang),
        }
        phase_label = phase_labels.get(phase, t("progress_processing", lang))

        spinner = cls._get_spinner()
        duration = cls._format_duration(elapsed_sec)
        content_formatted = f"{content_length:,}"
        model_short = cls._short_model(model)
        estimate = cls._estimate_seconds(content_length)
        bar = cls._progress_bar(elapsed_sec, estimate_sec=estimate)
        eta_str = cls._format_duration(estimate)

        # Tier display with icon
        tier_icons = {
            "technical": "\U0001f52c",
            "sociopolitical": "\U0001f30d",
            "default": "\U0001f4c4",
        }
        tier_icon = tier_icons.get(content_tier or "default", "\U0001f4c4")
        tier_label = (content_tier or "general").capitalize()

        # Language display
        lang_map = {"en": "English", "ru": "Russian"}
        lang_label = lang_map.get(content_lang or "", content_lang or "auto")

        lines = [
            f"\U0001f9e0 <b>{t('progress_ai_analysis', lang)}</b>",
            "",
            f"\U0001f4dd {t('progress_content', lang)}: {content_formatted} chars",
            f"\U0001f310 {t('progress_lang', lang)}: {lang_label}",
            f"{tier_icon} {t('progress_tier', lang)}: {tier_label}",
            f"\U0001f916 {t('progress_model', lang)}: <code>{html.escape(model_short)}</code>",
            "",
            f"\u23f1\ufe0f {phase_label} ({duration} / ~{eta_str}) {spinner}",
            f"<code>{bar}</code>",
            "",
            f"<i>{t('progress_status_processing', lang)}</i>",
        ]

        return "\n".join(lines)

    @classmethod
    def format_llm_complete(
        cls,
        model: str,
        elapsed_sec: float,
        success: bool = True,
        error_msg: str | None = None,
        correlation_id: str | None = None,
        lang: str = "en",
    ) -> str:
        """Format LLM completion message (HTML).

        Args:
            model: LLM model name
            elapsed_sec: Total elapsed time in seconds
            success: Whether analysis succeeded
            error_msg: Error message (if failed)
            correlation_id: Correlation ID for error tracking
            lang: UI language code ("en" or "ru").

        Returns:
            HTML-formatted completion message
        """
        duration = cls._format_duration(elapsed_sec)

        if success:
            return (
                f"\u2705 <b>{t('progress_analysis_complete', lang)}</b> ({duration})\n\n"
                f"\U0001f4ca {t('progress_summary_generated', lang)}\n"
                f"\U0001f916 {t('progress_model', lang)}: {html.escape(model)}"
            )
        error_text = html.escape(error_msg or "Unknown error")
        error_id_line = (
            f"\n{t('progress_error_id', lang)}: <code>{correlation_id}</code>"
            if correlation_id
            else ""
        )
        return f"\u274c <b>{t('progress_analysis_failed', lang)}</b> ({duration})\n\n{t('progress_error', lang)}: {error_text}{error_id_line}"

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
