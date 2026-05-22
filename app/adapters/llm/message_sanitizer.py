"""Shared message sanitization helpers for LLM request logging."""

from __future__ import annotations

from typing import Any

from app.core.logging_utils import bounded_debug_preview, redact_for_logging


def sanitize_messages_for_logging(
    messages: list[dict[str, Any]],
    *,
    content_limit: int = 1000,
    allow_debug_content: bool = False,
) -> list[dict[str, Any]]:
    """Return sanitized message copies safe for logs and persistence."""
    sanitized: list[dict[str, Any]] = []
    for message in messages:
        sanitized_message = dict(message)
        content = sanitized_message.get("content", "")
        if allow_debug_content:
            sanitized_message["content"] = bounded_debug_preview(
                content,
                max_chars=content_limit,
            )
        else:
            sanitized_message["content"] = redact_for_logging(content, key="content")
        sanitized.append(sanitized_message)
    return sanitized
