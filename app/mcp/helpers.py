from __future__ import annotations

import json
import logging
from typing import Any, TypedDict

logger = logging.getLogger("ratatoskr.mcp")


class McpErrorResult(TypedDict):
    """Standard error envelope returned by MCP service methods on failure."""

    error: str


def ensure_mapping(value: Any) -> dict[str, Any]:
    """Safely coerce a value to a dict (handles None, str JSON, etc.)."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError) as exc:
            logger.debug("mcp_ensure_mapping_parse_failed", extra={"error": str(exc)})
            return {}
    return {}


def isotime(dt: Any) -> str:
    """Convert a datetime to ISO 8601 string."""
    if dt is None:
        return ""
    if hasattr(dt, "isoformat"):
        text = dt.isoformat()
        if text.endswith("Z"):
            return text
        if text.endswith("+00:00"):
            return f"{text[:-6]}Z"
        if getattr(dt, "tzinfo", None) is None:
            return f"{text}Z"
        return text
    return str(dt) if dt else ""


def format_summary_compact(summary_row: Any, request_row: Any) -> dict[str, Any]:
    """Build a compact summary dict from ORM rows."""
    payload = ensure_mapping(getattr(summary_row, "json_payload", None))
    metadata = ensure_mapping(payload.get("metadata"))

    return {
        "summary_id": summary_row.id,
        "request_id": getattr(request_row, "id", None),
        "url": getattr(request_row, "input_url", "") or getattr(request_row, "normalized_url", ""),
        "title": metadata.get("title", "Untitled"),
        "domain": metadata.get("domain", ""),
        "summary_250": payload.get("summary_250", ""),
        "tldr": payload.get("tldr", ""),
        "topic_tags": payload.get("topic_tags", []),
        "reading_time_min": payload.get("estimated_reading_time_min", 0),
        "lang": getattr(summary_row, "lang", "auto"),
        "is_read": getattr(summary_row, "is_read", False),
        "is_favorited": getattr(summary_row, "is_favorited", False),
        "created_at": isotime(getattr(summary_row, "created_at", None)),
    }


def format_summary_detail(summary_row: Any, request_row: Any) -> dict[str, Any]:
    """Build a detailed summary dict from ORM rows."""
    payload = ensure_mapping(getattr(summary_row, "json_payload", None))
    metadata = ensure_mapping(payload.get("metadata"))
    entities = ensure_mapping(payload.get("entities"))
    readability = ensure_mapping(payload.get("readability"))

    return {
        "summary_id": summary_row.id,
        "request_id": getattr(request_row, "id", None),
        "url": getattr(request_row, "input_url", "") or getattr(request_row, "normalized_url", ""),
        "title": metadata.get("title", "Untitled"),
        "domain": metadata.get("domain", ""),
        "author": metadata.get("author"),
        "summary_250": payload.get("summary_250", ""),
        "summary_1000": payload.get("summary_1000", ""),
        "tldr": payload.get("tldr", ""),
        "key_ideas": payload.get("key_ideas", []),
        "topic_tags": payload.get("topic_tags", []),
        "entities": {
            "people": entities.get("people", []),
            "organizations": entities.get("organizations", []),
            "locations": entities.get("locations", []),
        },
        "estimated_reading_time_min": payload.get("estimated_reading_time_min", 0),
        "key_stats": payload.get("key_stats", []),
        "answered_questions": payload.get("answered_questions", []),
        "readability": (
            {
                "method": readability.get("method", ""),
                "score": readability.get("score", 0.0),
                "level": readability.get("level", ""),
            }
            if readability
            else None
        ),
        "seo_keywords": payload.get("seo_keywords", []),
        "lang": getattr(summary_row, "lang", "auto"),
        "is_read": getattr(summary_row, "is_read", False),
        "is_favorited": getattr(summary_row, "is_favorited", False),
        "created_at": isotime(getattr(summary_row, "created_at", None)),
        "request_status": getattr(request_row, "status", ""),
        "request_type": getattr(request_row, "type", ""),
    }


def paginated_payload(
    *,
    results: list[dict[str, Any]],
    total: int,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    return {
        "results": results,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": (offset + len(results)) < total,
    }


def clamp_limit(limit: int, *, minimum: int = 1, maximum: int = 25) -> int:
    return max(minimum, min(maximum, int(limit)))


def clamp_similarity(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def to_json(payload: Any) -> str:
    return json.dumps(payload, default=str)
