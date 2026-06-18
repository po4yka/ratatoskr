"""Dependency-light summary payload text extraction for embedding writers."""

from __future__ import annotations

import json
from typing import Any


def coerce_summary_payload(
    json_payload: str | dict[str, Any] | None,
) -> tuple[dict[str, Any], str | None]:
    """Parse a summary json_payload once for embedding text extraction."""
    if not json_payload:
        return {}, None
    if isinstance(json_payload, str):
        try:
            parsed = json.loads(json_payload)
        except (json.JSONDecodeError, ValueError):
            return {}, json_payload[:2000]
        return (parsed if isinstance(parsed, dict) else {}), None
    return (json_payload if isinstance(json_payload, dict) else {}), None


def extract_indexable_text(payload: dict[str, Any], *, raw_fallback: str | None = None) -> str:
    """Extract the text embedded for a summary payload."""
    if not payload:
        return raw_fallback or ""

    parts: list[str] = []
    metadata = payload.get("metadata", {}) if isinstance(payload.get("metadata"), dict) else {}
    title = metadata.get("title") or payload.get("title") or ""
    if title:
        parts.append(title)
    for key in ("summary_1000", "summary_250", "tldr"):
        val = payload.get(key)
        if val and isinstance(val, str):
            parts.append(val)
            break
    key_ideas = payload.get("key_ideas")
    if isinstance(key_ideas, list):
        parts.extend(str(k) for k in key_ideas[:5] if k)
    tags = payload.get("topic_tags")
    if isinstance(tags, list):
        parts.append(" ".join(str(t) for t in tags[:10] if t))
    return " ".join(parts)[:4000]
