"""DTOs for vector search and unified retrieval results."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


@dataclass(frozen=True, slots=True)
class VectorSearchHitDTO:
    request_id: int
    summary_id: int
    similarity_score: float
    url: str | None
    title: str | None
    snippet: str | None
    source: str | None = None
    published_at: str | None = None


class EntityType(StrEnum):
    """Retrievable entity discriminator for the unified retrieval port (ADR-0016).

    Values match the Qdrant payload ``entity_type`` written by the fast path and
    the reconciler (``"summary"`` / ``"repository"``); the single Qdrant adapter
    maps each to its filter + hydration path.
    """

    SUMMARY = "summary"
    REPOSITORY = "repository"
    GIT_MIRROR = "git_mirror"
    X_WIKI = "x_wiki"


@dataclass(frozen=True, slots=True)
class RetrievalScope:
    """Mandatory server-side scope filter for every retrieval call.

    Centralizes the IDOR / tenant guard (CLAUDE.md rule 12, ADR-0005/0012). The
    retrieval port REQUIRES a scope on every call, and the Qdrant adapter
    unconditionally injects ``environment`` + ``user_scope`` into the Qdrant
    filter, so no caller can structurally omit them. ``environment`` and
    ``user_scope`` are required (no defaults) -- a scope cannot be constructed
    without them -- and must be the canonical (lowercased) values the index was
    written with, since the adapter matches them verbatim.

    ``user_id`` is REQUIRED for repository / git_mirror retrieval (the adapter
    raises without it) and OPTIONAL for summary, which is owner-wide when it is
    None, matching the legacy StoreVectorSearchService. x_wiki is shared content
    and is not user-partitioned.
    """

    environment: str
    user_scope: str
    user_id: int | None = None


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    """One neutral, entity-agnostic hit returned by :class:`RetrievalPort`.

    Carries BOTH ``score`` (cosine similarity in ``[0, 1]``, 1 = identical) and
    ``distance`` (``1 - score``) so every legacy caller can reproduce its exact
    convention at cutover -- summaries emit ``similarity_score = score``;
    repository / git-mirror emit ``distance``; MCP emits ``round(score, 4)``.
    ``hydrated`` holds the Postgres row (column dict) for entity types hydrated
    from the DB (repository / git_mirror); it is ``None`` for summary / x_wiki
    where the Qdrant ``payload`` already carries everything.
    """

    entity_type: EntityType
    entity_id: str
    point_id: str
    score: float
    distance: float
    payload: dict[str, Any]
    hydrated: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """Ordered hits for a single retrieval call (Qdrant rank order preserved).

    ``total`` is the number of hits IN THIS RESULT (post-hydration / post-rerank),
    not a pre-pagination candidate count. The port does not page server-side: a
    caller needing ``offset`` / ``has_more`` over-fetches via ``top_k`` and slices,
    the way the legacy services did (top_k = limit + offset + buffer).
    """

    hits: list[RetrievalHit]
    total: int
