"""Single source of truth for a summary's Qdrant point shape (ADR-0012).

Both the Taskiq reconciler (``ratatoskr.vector.reconcile``, convergence/backfill)
and the synchronous read-your-writes fast-path
(:mod:`app.infrastructure.vector.summary_index_adapter`, freshness) build the
SAME point from a summary payload here, so the two writers never disagree and
the reconciler sees no drift. Keep this module dependency-light -- the fast-path
runs in the request hot path.
"""

from typing import Any

from app.core.summary_embedding_text import coerce_summary_payload, extract_indexable_text

__all__ = [
    "build_summary_qdrant_payload",
    "coerce_summary_payload",
    "extract_indexable_text",
]


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
