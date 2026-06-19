"""Shared types and formatting helpers for export connectors."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ExportPayload:
    summary_id: int
    request_id: int | None
    url: str | None
    title: str
    tldr: str
    summary_250: str
    topic_tags: list[str] = field(default_factory=list)
    highlights: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExportResult:
    success: bool
    response_status: int | None = None
    response_body: str | None = None
    error: str | None = None


def payload_from_summary_context(context: dict[str, Any]) -> ExportPayload:
    summary = _mapping(context.get("summary"))
    request = _mapping(context.get("request"))
    payload = _mapping(summary.get("json_payload"))
    metadata = _mapping(payload.get("metadata"))
    title = str(
        metadata.get("title")
        or payload.get("title")
        or request.get("input_url")
        or f"Summary {summary.get('id')}"
    )
    topic_tags = [str(item) for item in payload.get("topic_tags") or [] if str(item).strip()]
    highlights = [str(item) for item in payload.get("key_ideas") or [] if str(item).strip()]
    if not highlights and isinstance(payload.get("extractive_quotes"), list):
        highlights = [str(item) for item in payload["extractive_quotes"] if str(item).strip()]
    return ExportPayload(
        summary_id=int(summary["id"]),
        request_id=_optional_int(summary.get("request_id") or request.get("id")),
        url=_optional_str(request.get("normalized_url") or request.get("input_url")),
        title=title,
        tldr=str(payload.get("tldr") or ""),
        summary_250=str(payload.get("summary_250") or ""),
        topic_tags=topic_tags,
        highlights=highlights,
        raw=payload,
    )


def render_markdown(payload: ExportPayload) -> str:
    lines = [f"# {payload.title}", ""]
    if payload.url:
        lines.extend([f"Source: {payload.url}", ""])
    if payload.topic_tags:
        lines.extend(["Tags: " + ", ".join(payload.topic_tags), ""])
    if payload.tldr:
        lines.extend(["## TL;DR", "", payload.tldr, ""])
    if payload.summary_250:
        lines.extend(["## Summary", "", payload.summary_250, ""])
    if payload.highlights:
        lines.extend(["## Highlights", ""])
        lines.extend(f"- {item}" for item in payload.highlights)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def safe_markdown_filename(title: str, summary_id: int) -> str:
    stem = re.sub(r"[^a-zA-Z0-9._ -]+", "", title).strip().replace(" ", "-")
    stem = re.sub(r"-{2,}", "-", stem).strip("-._")[:80] or f"summary-{summary_id}"
    return f"{stem}-{summary_id}.md"


def ensure_child_path(root: Any, filename: str) -> Any:
    root_resolved = root.expanduser().resolve()
    path = (root_resolved / filename).resolve()
    if root_resolved not in path.parents and path != root_resolved:
        msg = "Export path escapes configured Obsidian vault"
        raise ValueError(msg)
    return path


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _optional_str(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    return None
