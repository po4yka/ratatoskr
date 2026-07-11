"""Bounded citation-first research graph over a personal archive.

The graph is deliberately framework-independent: its five named phases are plain async methods, so the MCP surface can use it without requiring a second LLM or graph-runtime dependency. Adapters provide scoped discovery and hydration; this module enforces the bounded evidence and citation contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence


_MAX_SOURCES = 12
_SOURCE_KINDS = frozenset(
    {"summary", "repository", "x_bookmark", "git_mirror", "highlight", "note"}
)


@dataclass(frozen=True, slots=True)
class ArchiveEvidence:
    """One scoped archive item that may become a citation after hydration."""

    source_kind: str
    source_id: str
    title: str
    excerpt: str
    url: str | None
    score: float = 0.0


class ArchiveResearchSources(Protocol):
    """Scoped discovery and hydration seam for the archive research graph."""

    async def retrieve(self, *, query: str, per_source_limit: int) -> Sequence[ArchiveEvidence]:
        """Return scored candidate evidence from supported archive source kinds."""
        ...

    async def hydrate(self, evidence: ArchiveEvidence) -> ArchiveEvidence:
        """Return citation-ready evidence, or an evidence item with no excerpt."""
        ...


class CitationFirstArchiveResearchGraph:
    """Run the bounded ``plan → retrieve → hydrate → synthesize → verify`` workflow."""

    def __init__(self, sources: ArchiveResearchSources) -> None:
        self._sources = sources

    async def run(self, query: str, *, max_sources: int = _MAX_SOURCES) -> dict[str, Any]:
        """Answer from hydrated archive evidence and return only verified citations."""
        plan = self._plan(query, max_sources)
        candidates = await self._retrieve(plan)
        evidence = await self._hydrate(candidates, max_sources=int(plan["max_sources"]))
        answer, claims = self._synthesize(str(plan["query"]), evidence)
        citations = self._verify_citations(claims, evidence)
        return {
            "answer": answer,
            "citations": citations,
            "citation_count": len(citations),
            "plan": plan,
        }

    @staticmethod
    def _plan(query: str, max_sources: int) -> dict[str, int | str]:
        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("query must not be blank")
        bounded_sources = max(1, min(_MAX_SOURCES, int(max_sources)))
        return {
            "query": normalized_query,
            "max_sources": bounded_sources,
            "per_source_limit": min(3, bounded_sources),
        }

    async def _retrieve(self, plan: dict[str, int | str]) -> list[ArchiveEvidence]:
        candidates = await self._sources.retrieve(
            query=str(plan["query"]),
            per_source_limit=int(plan["per_source_limit"]),
        )
        seen: set[str] = set()
        selected: list[ArchiveEvidence] = []
        for candidate in sorted(candidates, key=lambda item: item.score, reverse=True):
            if candidate.source_kind not in _SOURCE_KINDS or candidate.source_id in seen:
                continue
            seen.add(candidate.source_id)
            selected.append(candidate)
            if len(selected) == int(plan["max_sources"]):
                break
        return selected

    async def _hydrate(
        self, candidates: Sequence[ArchiveEvidence], *, max_sources: int
    ) -> list[ArchiveEvidence]:
        hydrated: list[ArchiveEvidence] = []
        for candidate in candidates[:max_sources]:
            item = await self._sources.hydrate(candidate)
            if item.excerpt.strip():
                hydrated.append(item)
        return hydrated

    @staticmethod
    def _synthesize(
        query: str, evidence: Sequence[ArchiveEvidence]
    ) -> tuple[str, list[tuple[str, str]]]:
        if not evidence:
            return "No citation-ready evidence was found in your archive.", []
        lines = [f"Evidence-led answer for: {query}"]
        claims: list[tuple[str, str]] = []
        for item in evidence:
            claim = item.excerpt.strip()
            claims.append((claim, item.source_id))
            lines.append(f"- {claim} [{item.source_id}]")
        return "\n".join(lines), claims

    @staticmethod
    def _verify_citations(
        claims: Sequence[tuple[str, str]], evidence: Sequence[ArchiveEvidence]
    ) -> list[dict[str, str | None]]:
        evidence_by_id = {item.source_id: item for item in evidence}
        citations: list[dict[str, str | None]] = []
        for claim, source_id in claims:
            item = evidence_by_id.get(source_id)
            if item is None or claim != item.excerpt.strip():
                continue
            citations.append(
                {
                    "id": item.source_id,
                    "kind": item.source_kind,
                    "title": item.title,
                    "url": item.url,
                }
            )
        return citations
