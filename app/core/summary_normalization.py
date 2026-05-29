"""Pure normalisation utilities for LLM summary field names.

This module is part of ``app.core`` so that application-layer services can
import it without violating the ``application-no-outward`` contract.
"""

from __future__ import annotations

from typing import Any

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
    """Standardise varied LLM field names into the canonical summary format."""
    return {_CANONICAL_METRIC_NAMES.get(k.lower(), k): v for k, v in metrics.items()}


__all__ = ["normalize_metric_names"]
