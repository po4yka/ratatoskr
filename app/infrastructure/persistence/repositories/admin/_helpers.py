"""Shared helper utilities for admin read repositories.

These are pure functions (no DB access) used across the bounded-context
sub-repositories: redaction, enum coercion, JSON parsing, and date parsing.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

from app.core.time_utils import UTC, isotime

__all__ = [
    "_SECRET_PATTERNS",
    "_enum_value",
    "_first_error",
    "_parse_github_sync_state",
    "_parse_since",
    "_redact_match",
    "_redact_message",
    "_safe_social_attempt_metadata",
    "isotime",
]


def _parse_since(since: str | None) -> dt.datetime | None:
    if not since:
        return None
    normalized = since.removesuffix("Z")
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _enum_value(value: Any) -> str:
    return str(value.value if hasattr(value, "value") else value or "unknown")


def _safe_social_attempt_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "api_status",
        "api_supported_for_url",
        "provider_resource_id",
        "provider_shortcode",
        "unsupported_reason",
    }
    safe = {key: metadata[key] for key in allowed if key in metadata}
    rate_limit = metadata.get("rate_limit")
    if isinstance(rate_limit, dict):
        safe["rate_limit"] = {
            key: rate_limit[key]
            for key in ("limit", "remaining", "reset", "reset_at")
            if key in rate_limit
        }
    return safe


_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(authorization)\s*=\s*(bearer|basic)\s+[a-z0-9._~+/=-]+"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization)=([^&\s]+)"),
    re.compile(r"(?i)\b(bearer|basic)\s+[a-z0-9._~+/=-]+"),
    re.compile(r"(?i)(sk-[a-z0-9_-]{12,})"),
)


def _redact_message(message: Any, *, max_len: int = 240) -> str | None:
    if message is None:
        return None
    text = str(message)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_redact_match, text)
    redaction_end = text.find("[REDACTED]")
    if redaction_end >= 0:
        text = text[: redaction_end + len("[REDACTED]")]
    text = text.replace("\n", " ").replace("\r", " ").strip()
    if len(text) > max_len:
        return f"{text[: max_len - 3]}..."
    return text or None


def _redact_match(match: re.Match[str]) -> str:
    if match.lastindex == 1:
        return "[REDACTED]"
    return f"{match.group(1)}=[REDACTED]"


def _parse_github_sync_state(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("kind") != "github_sync_state":
        return {}
    return data


def _first_error(errors_json: Any) -> str | None:
    if isinstance(errors_json, list) and errors_json:
        return str(errors_json[0])
    if isinstance(errors_json, dict):
        for key in ("message", "error", "detail"):
            value = errors_json.get(key)
            if value:
                return str(value)
    return None
