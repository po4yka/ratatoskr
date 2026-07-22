"""Stateless data formatting operations.

All formatting methods return HTML-safe strings with proper escaping and markup.
Numeric values are wrapped in <code> tags for visual distinction.
"""

from __future__ import annotations

import html
import math
from typing import Any

from app.core.ui_strings import t


class DataFormatterImpl:
    """Stateless implementation of data formatting operations.

    Args:
        lang: UI language code ("en" or "ru").
    """

    def __init__(self, lang: str = "en") -> None:
        self._lang = lang

    def format_bytes(self, size: int) -> str:
        """Convert byte count into a human-readable string."""
        units = ["B", "KB", "MB", "GB", "TB"]
        value = float(size)
        for unit in units:
            if value < 1024 or unit == units[-1]:
                if unit == "B":
                    return f"{int(value)} {unit}"
                return f"{value:.1f} {unit}"
            value /= 1024
        return f"{value:.1f} TB"

    def format_metric_value(self, value: Any) -> str | None:
        """Format metric values, trimming insignificant decimals and booleans."""
        if value is None:
            return None
        if isinstance(value, bool):
            return t("yes", self._lang) if value else t("no", self._lang)
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            if math.isnan(value) or math.isinf(value):
                return str(value)
            if value.is_integer():
                return str(int(value))
            return f"{value:.2f}".rstrip("0").rstrip(".")
        return str(value).strip()

    def format_key_stats(self, key_stats: list[dict[str, Any]]) -> list[str]:
        """Render key statistics into bullet-point lines with HTML formatting.

        Returns HTML-formatted strings where:
        - Labels are HTML-escaped
        - Numeric values are wrapped in <code> tags
        - Units are appended after numeric values
        - Example: "• Revenue: <code>$1.2B</code>"

        Args:
            key_stats: List of stats dicts with 'label', 'value', 'unit', 'source_excerpt'.

        Returns:
            List of HTML-formatted bullet-point strings.
        """
        formatted: list[str] = []
        for entry in key_stats:
            if not isinstance(entry, dict):
                continue

            label = str(entry.get("label", "")).strip()
            if not label:
                continue

            # Escape label for HTML
            label_escaped = html.escape(label)

            value_text = self.format_metric_value(entry.get("value"))
            # `or ""`, not a .get() default: the model emits an explicit null
            # unit for dimensionless stats (years, counts). A default only fires
            # when the key is absent, so str(None) rendered a literal "None"
            # after the value -- "1958 None".
            unit = str(entry.get("unit") or "").strip()
            source_excerpt = str(entry.get("source_excerpt", "")).strip()

            detail_parts: list[str] = []
            if value_text is not None:
                # Wrap numeric value in <code> tags
                value_code = f"<code>{html.escape(value_text)}</code>"
                if unit:
                    # Append unit after the code tag
                    detail_parts.append(f"{value_code} {html.escape(unit)}")
                else:
                    detail_parts.append(value_code)
            elif unit:
                detail_parts.append(html.escape(unit))

            if source_excerpt:
                # Escape source excerpt as well
                detail_parts.append(f"{t('source', self._lang)}: {html.escape(source_excerpt)}")

            if detail_parts:
                formatted.append(f"• {label_escaped}: " + " — ".join(detail_parts))
            else:
                formatted.append(f"• {label_escaped}")

        return formatted

    def format_key_stats_compact(self, key_stats: list[dict[str, Any]]) -> list[str]:
        """Render key statistics into short bullet-point lines.

        This is intended for compact "card" UIs where source excerpts are too verbose.

        Example:
            "• GPS satellite speed: <code>14000</code> km/h"
        """
        formatted: list[str] = []
        for entry in key_stats:
            if not isinstance(entry, dict):
                continue

            label = str(entry.get("label", "")).strip()
            if not label:
                continue

            label_escaped = html.escape(label)
            value_text = self.format_metric_value(entry.get("value"))
            # `or ""`, not a .get() default: the model emits an explicit null
            # unit for dimensionless stats (years, counts). A default only fires
            # when the key is absent, so str(None) rendered a literal "None"
            # after the value -- "1958 None".
            unit = str(entry.get("unit") or "").strip()

            detail_parts: list[str] = []
            if value_text is not None:
                value_code = f"<code>{html.escape(value_text)}</code>"
                if unit:
                    detail_parts.append(f"{value_code} {html.escape(unit)}")
                else:
                    detail_parts.append(value_code)
            elif unit:
                detail_parts.append(html.escape(unit))

            if detail_parts:
                formatted.append(f"• {label_escaped}: " + " ".join(detail_parts))
            else:
                formatted.append(f"• {label_escaped}")

        return formatted

    def format_readability(self, readability: Any) -> str | None:
        """Create a reader-friendly readability summary line with HTML formatting.

        Supports both single dict and list of dicts for multiple methods.

        Returns HTML-formatted string where:
        - Method name is displayed (e.g., "Flesch-Kincaid")
        - Score (numeric) is wrapped in <code> tags
        - Level is displayed (e.g., "College")

        Args:
            readability: Dict or List of dicts with 'method', 'score', 'level'.

        Returns:
            HTML-formatted readability summary string, or None if no valid data.
        """
        if not readability:
            return None

        entries = readability if isinstance(readability, list) else [readability]
        formatted_entries: list[str] = []

        for entry in entries:
            if not isinstance(entry, dict):
                continue

            method_raw = str(entry.get("method", "")).strip()
            method_display = (
                html.escape(method_raw[:1].upper() + method_raw[1:]) if method_raw else ""
            )

            score = self.format_metric_value(entry.get("score"))
            level_raw = str(entry.get("level", "")).strip()
            level_display = html.escape(level_raw[:1].upper() + level_raw[1:]) if level_raw else ""

            detail_parts: list[str] = []
            if score is not None:
                score_code = f"<code>{html.escape(score)}</code>"
                detail_parts.append(f"{t('score', self._lang)}: {score_code}")
            if level_display:
                detail_parts.append(f"{t('level', self._lang)}: {level_display}")

            details = " • ".join(detail_parts)
            if method_display and details:
                formatted_entries.append(f"{method_display}: {details}")
            elif method_display:
                formatted_entries.append(method_display)
            elif details:
                formatted_entries.append(details)

        if not formatted_entries:
            return None

        return " | ".join(formatted_entries)

    def normalize_metric_names(self, metrics: dict[str, Any]) -> dict[str, Any]:
        """Standardize varied field names from different LLMs into a canonical format.

        Common variations handled:
        - reading_time -> estimated_reading_time_min
        - complexity -> readability_score
        - word_count -> word_count_approx
        """
        mapping = {
            "reading_time": "estimated_reading_time_min",
            "time_to_read": "estimated_reading_time_min",
            "complexity": "readability_score",
            "readability": "readability_score",
            "words": "word_count_approx",
            "word_count": "word_count_approx",
            "lang": "language",
            "detected_language": "language",
        }

        normalized = {}
        for key, value in metrics.items():
            canonical_key = mapping.get(key.lower(), key)
            normalized[canonical_key] = value

        return normalized

    def format_firecrawl_options(self, options: dict[str, Any] | None) -> str | None:
        """Format Firecrawl options into a display string."""
        if not isinstance(options, dict) or not options:
            return None

        parts: list[str] = []

        mobile = options.get("mobile")
        if isinstance(mobile, bool):
            parts.append("mobile=on" if mobile else "mobile=off")

        formats = options.get("formats")
        if isinstance(formats, list | tuple):
            fmt_values = [str(v).strip() for v in formats if str(v).strip()]
            if fmt_values:
                parts.append("formats=" + ", ".join(fmt_values[:5]))

        parsers = options.get("parsers")
        if isinstance(parsers, list | tuple):
            parser_values = [str(v).strip() for v in parsers if str(v).strip()]
            if parser_values:
                parts.append("parsers=" + ", ".join(parser_values[:5]))

        for key, value in options.items():
            if key in {"mobile", "formats", "parsers"}:
                continue
            if isinstance(value, bool):
                parts.append(f"{key}={'on' if value else 'off'}")
            elif isinstance(value, int | float):
                parts.append(f"{key}={value}")
            elif isinstance(value, str):
                clean = value.strip()
                if clean:
                    parts.append(f"{key}={clean}")
            elif isinstance(value, list | tuple):
                clean_values = [str(v).strip() for v in value if str(v).strip()]
                if clean_values:
                    parts.append(f"{key}=" + ", ".join(clean_values[:5]))

        if not parts:
            return None

        return "; ".join(parts)


_CANONICAL_METRIC_NAMES: dict[str, str] = {
    "reading_time": "estimated_reading_time_min",
    "time_to_read": "estimated_reading_time_min",
    "complexity": "readability_score",
    "readability": "readability_score",
    "words": "word_count_approx",
    "word_count": "word_count_approx",
    "lang": "language",
    "detected_language": "language",
}


def normalize_metric_names(metrics: dict[str, Any]) -> dict[str, Any]:
    """Standardize varied LLM field names into canonical format."""
    return {_CANONICAL_METRIC_NAMES.get(k.lower(), k): v for k, v in metrics.items()}
