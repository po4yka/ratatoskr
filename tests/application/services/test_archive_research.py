from __future__ import annotations

import pytest

from app.application.services.archive_research import (
    ArchiveEvidence,
    CitationFirstArchiveResearchGraph,
)


class _ArchiveSources:
    async def retrieve(self, *, query: str, per_source_limit: int) -> list[ArchiveEvidence]:
        assert query == "How does checkpoint durability work?"
        assert per_source_limit == 3
        return [
            ArchiveEvidence(
                source_kind="summary",
                source_id="summary:7",
                title="Checkpoint durability",
                excerpt="A saver persists graph state between nodes.",
                url="https://example.com/checkpoints",
                score=0.9,
            ),
            ArchiveEvidence(
                source_kind="note",
                source_id="note:3",
                title="My note",
                excerpt="Use the Postgres saver in production graphs.",
                url=None,
                score=0.8,
            ),
        ]

    async def hydrate(self, evidence: ArchiveEvidence) -> ArchiveEvidence:
        return evidence


@pytest.mark.asyncio
async def test_research_graph_returns_only_hydrated_evidence_as_citations() -> None:
    result = await CitationFirstArchiveResearchGraph(_ArchiveSources()).run(
        "How does checkpoint durability work?",
        max_sources=6,
    )

    assert result["plan"] == {
        "query": "How does checkpoint durability work?",
        "max_sources": 6,
        "per_source_limit": 3,
    }
    assert result["citation_count"] == 2
    assert result["citations"] == [
        {
            "id": "summary:7",
            "kind": "summary",
            "title": "Checkpoint durability",
            "url": "https://example.com/checkpoints",
        },
        {
            "id": "note:3",
            "kind": "note",
            "title": "My note",
            "url": None,
        },
    ]
    assert "[summary:7]" in result["answer"]
    assert "[note:3]" in result["answer"]
