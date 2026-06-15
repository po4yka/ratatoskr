"""Unified vector retrieval (ADR-0016).

One Qdrant-backed adapter implements :class:`app.application.ports.retrieval.RetrievalPort`
and is the single place the mandatory ``environment`` + ``user_scope`` (+ per-entity
``user_id``) scope filter is built -- converging the previously divergent
filter-build sites in StoreVectorSearchService / RepositorySearchService /
GitMirrorSearchService / MCP SemanticSearchService.
"""

from __future__ import annotations

from app.infrastructure.retrieval.qdrant_retrieval_adapter import QdrantRetrievalAdapter

__all__ = ["QdrantRetrievalAdapter"]
