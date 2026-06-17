"""Single source of truth for a summary's Qdrant point shape (ADR-0012).

Both the Taskiq reconciler (``ratatoskr.vector.reconcile``, convergence/backfill)
and the synchronous read-your-writes fast-path
(:mod:`app.infrastructure.vector.summary_index_adapter`, freshness) build the
SAME point from a summary payload here, so the two writers never disagree and
the reconciler sees no drift. Keep this module dependency-light (``json`` +
stdlib only) -- the fast-path runs in the request hot path.
"""

from __future__ import annotations

import json
from typing import Any


def coerce_summary_payload(
    json_payload: str | dict[str, Any] | None,
) -> tuple[dict[str, Any], str | None]:
    """Parse a summary's json_payload once.

    Returns ``(payload_dict, raw_fallback)``: ``payload_dict`` is the parsed
    object (``{}`` when absent, non-object, or unparseable), and
    ``raw_fallback`` is the truncated raw string to embed *only* when a string
    payload failed to parse (``None`` otherwise) -- preserving the original
    text-extraction fallback without re-parsing in each helper.
    """
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
    """Extract the text we embed from a parsed summary payload.

    Mirrors the logic in app.core.embedding_text.prepare_text_for_embedding
    but operates on the parsed payload without the token-length truncation
    (batching/chunking are not done here; one point per summary). The fast-path MUST
    embed this exact text (not prepare_text_for_embedding) or the vector would
    diverge from the shared point shape (summary_point.py) for the same summary.
    """
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


def build_summary_qdrant_payload(
    summary_id: int,
    request_id: int,
    lang: str | None,
    payload: dict[str, Any],
    user_scope: str,
    environment: str,
) -> dict[str, Any]:
    """Build the Qdrant point payload dict from a parsed summary payload.

    Must be compatible with the payload schema produced by
    app.infrastructure.vector.metadata_builder.MetadataBuilder so that the
    existing query() path keeps working, and IDENTICAL between the reconciler
    and the read-your-writes fast-path so the reconciler reports no drift.
    """
    metadata = payload.get("metadata", {}) if isinstance(payload.get("metadata"), dict) else {}
    return {
        "entity_type": "summary",
        "summary_id": summary_id,
        "request_id": request_id,
        "language": lang or "en",
        "user_scope": user_scope,
        "environment": environment,
        "title": metadata.get("title") or payload.get("title") or "",
        "url": metadata.get("url") or payload.get("url") or "",
        "source_type": payload.get("source_type") or "",
        "tldr": payload.get("tldr") or "",
        "topic_tags": payload.get("topic_tags") or [],
        "summary_250": payload.get("summary_250") or "",
    }
