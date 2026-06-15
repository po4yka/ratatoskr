"""Composition root for the unified retrieval adapter (ADR-0016 / ADR-0010).

Builds the single Qdrant-backed ``RetrievalPort`` implementation. This is the
seam the graph ``ground`` node (ADR-0005/0012) and -- at cutover -- the API
search service, MCP context, and git_mirrors router are injected with.

Cutover (migrating the five legacy services onto this adapter, the golden
byte-stable parity tests, and the port-only import-linter contract) is the
parity-gated follow-up; this builder is wired but not yet injected into the
runtime bundles, so existing behavior is unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.infrastructure.retrieval import QdrantRetrievalAdapter

if TYPE_CHECKING:
    from app.application.ports.retrieval import RetrievalPort


def build_retrieval_adapter(
    *,
    vector_store: Any,
    embedding_service: Any,
    db: Any,
    reranker: Any | None = None,
    query_expansion: Any | None = None,
) -> RetrievalPort:
    """Construct the single Qdrant-backed :class:`RetrievalPort` implementation.

    ``reranker`` and ``query_expansion`` are optional: when omitted, ``retrieve``
    with ``rerank=False`` / ``expand_query=False`` (the defaults) reproduces the
    un-reranked, un-expanded legacy ordering.
    """
    return QdrantRetrievalAdapter(
        vector_store=vector_store,
        embedding_service=embedding_service,
        db=db,
        reranker=reranker,
        query_expansion=query_expansion,
    )
