"""Shared context builder for summary evaluation.

Used by both the rule engine and smart collections to build
the evaluation context dict from summary and request data.
"""

from __future__ import annotations

import json
from typing import Any

# Sentinel: a value that is explicitly present in the dict but is None/falsy,
# distinct from a key that is simply absent.
_MISSING = object()


def build_summary_context(
    summary_dict: dict[str, Any] | None,
    request_dict: dict[str, Any] | None,
    tag_names: list[str] | None = None,
) -> dict[str, Any]:
    """Build evaluation context from summary and request data.

    Returns dict with keys: url, title, tags, language, reading_time,
    source_type, content.

    Metadata fields (title, source_type, reading_time, tags) are sourced from
    the denormalized scalar columns added in migration 0030 when they are
    present in *summary_dict* (i.e. the key exists and the value is not None).
    For rows written before the migration or not yet backfilled the value will
    be None, so we fall back to extracting the same fields from json_payload.
    This makes the function safe across a rolling deployment.

    ``content`` (summary_1000 / summary_250) always comes from json_payload
    because it is intentionally not denormalized.
    """
    summary = summary_dict or {}

    # Always parse json_payload — needed for ``content`` and as fallback for
    # metadata on pre-migration rows.
    json_payload = summary.get("json_payload") or {}
    if isinstance(json_payload, str):
        try:
            json_payload = json.loads(json_payload)
        except (json.JSONDecodeError, TypeError):
            json_payload = {}

    # --- title ---
    # Use denormalized column when available (key present AND non-None),
    # fall back to payload.
    col_title = summary.get("title", _MISSING)
    if col_title is not _MISSING and col_title is not None:
        title: str = str(col_title)
    else:
        title = json_payload.get("title", "")

    # --- source_type ---
    col_source_type = summary.get("source_type", _MISSING)
    if col_source_type is not _MISSING and col_source_type is not None:
        source_type: str = str(col_source_type)
    else:
        source_type = json_payload.get("source_type", "")

    # --- reading_time ---
    col_reading_time = summary.get("reading_time", _MISSING)
    if col_reading_time is not _MISSING and col_reading_time is not None:
        try:
            reading_time: int = int(col_reading_time)
        except (TypeError, ValueError):
            reading_time = json_payload.get("estimated_reading_time_min", 0)
    else:
        reading_time = json_payload.get("estimated_reading_time_min", 0)

    # --- tags ---
    # tag_names (caller-supplied, e.g. from a separate Tag JOIN) takes
    # precedence; then the denormalized column; then the payload.
    if tag_names:
        tags: list[Any] = tag_names
    else:
        col_topic_tags = summary.get("topic_tags", _MISSING)
        if col_topic_tags is not _MISSING and isinstance(col_topic_tags, list):
            tags = col_topic_tags
        else:
            tags = json_payload.get("topic_tags", [])

    return {
        "url": (request_dict or {}).get("normalized_url")
        or (request_dict or {}).get("input_url", ""),
        "title": title,
        "tags": tags,
        "language": summary.get("lang", ""),
        "reading_time": reading_time,
        "source_type": source_type,
        # content always from payload — intentionally not denormalized.
        "content": json_payload.get("summary_1000", "") or json_payload.get("summary_250", ""),
    }
