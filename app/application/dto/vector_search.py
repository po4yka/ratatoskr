"""DTOs for vector search and unified retrieval results."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


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

    Values match the Qdrant payload ``entity_type`` written by the CocoIndex
    flows; T4 (unified retrieval) is the authority that maps each to its
    hydration path.
    """

    SUMMARY = "summary"
    REPOSITORY = "repository"
    GIT_MIRROR = "git_mirror"
    X_WIKI = "x_wiki"


@dataclass(frozen=True, slots=True)
class RetrievalScope:
    """Mandatory server-side scope filter for every retrieval call.

    Centralizes the IDOR / tenant guard (CLAUDE.md rule 12, ADR-0005/0012): the
    retrieval port REQUIRES a scope on every call, and the Qdrant adapter (T4)
    injects ``environment`` + ``user_scope`` (plus per-entity ``user_id`` for
    user-scoped entities) so no caller can structurally omit it.
    """

    environment: str
    user_scope: str | None = None
    user_id: int | None = None


@dataclass(frozen=True, slots=True)
class RetrievalHitDTO(VectorSearchHitDTO):
    """A retrieval hit: a :class:`VectorSearchHitDTO` tagged with its entity type.

    Extends (does not parallel) :class:`VectorSearchHitDTO` so summary-shaped
    hits keep their existing fields while gaining the ``entity_type``
    discriminator. T4 generalizes per-entity hydration on top of this seam.
    """

    entity_type: EntityType = EntityType.SUMMARY
