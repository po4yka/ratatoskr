"""Focused tests for multi-source aggregation heuristics."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.agents.multi_source_aggregation_agent import (
    MultiSourceAggregationAgent,
    MultiSourceAggregationInput,
    _SentenceCache,
)
from app.application.dto.aggregation import (
    ExtractedTextKind,
    NormalizedSourceDocument,
    SourceExtractionItemResult,
    SourceProvenance,
    SourceTextBlock,
)
from app.domain.models.source import AggregationItemStatus, SourceKind


def _agent() -> MultiSourceAggregationAgent:
    return MultiSourceAggregationAgent(aggregation_session_repo=AsyncMock())


def _document(
    source_item_id: str,
    text: str,
    *,
    title: str | None = None,
    kind: SourceKind = SourceKind.WEB_ARTICLE,
) -> NormalizedSourceDocument:
    return NormalizedSourceDocument(
        source_item_id=source_item_id,
        source_kind=kind,
        title=title,
        text=text,
        text_blocks=[
            SourceTextBlock(
                kind=ExtractedTextKind.BODY,
                text=text,
                position=0,
            )
        ],
        provenance=SourceProvenance(
            source_item_id=source_item_id,
            source_kind=kind,
        ),
    )


def _item(position: int, document: NormalizedSourceDocument) -> SourceExtractionItemResult:
    return SourceExtractionItemResult(
        position=position,
        item_id=position,
        source_item_id=document.source_item_id,
        source_kind=document.source_kind,
        status=AggregationItemStatus.EXTRACTED.value,
        normalized_document=document,
    )


def test_sentence_cache_reuses_document_splits_across_heuristics() -> None:
    agent = _agent()
    shared_sentence = "Shared operational signal confirms the same account migration window."
    items = [
        _item(
            1,
            _document(
                "src_one",
                f"{shared_sentence} The rollout affected 12 customer accounts today.",
            ),
        ),
        _item(
            2,
            _document(
                "src_two",
                f"{shared_sentence} The rollout affected 18 customer accounts today.",
            ),
        ),
    ]
    source_weights = [agent._build_source_weight(item) for item in items]
    sentence_cache = _SentenceCache()

    duplicate_signals = agent._detect_duplicate_signals(
        items,
        sentence_cache=sentence_cache,
    )
    contradiction_hints = agent._detect_contradiction_hints(
        items,
        sentence_cache=sentence_cache,
    )
    fallback_claims = agent._fallback_claims(
        items,
        source_weights,
        sentence_cache=sentence_cache,
    )

    assert [signal.summary for signal in duplicate_signals] == [shared_sentence]
    assert duplicate_signals[0].source_item_ids == ["src_one", "src_two"]
    assert len(contradiction_hints) == 1
    assert contradiction_hints[0].source_item_ids == ["src_one", "src_two"]
    assert [claim.source_item_ids for claim in fallback_claims] == [["src_one"], ["src_two"]]
    assert len(sentence_cache._documents) == 2
    assert len(sentence_cache._blocks) == 2


def test_sentence_cache_keeps_distinct_documents_with_same_content() -> None:
    agent = _agent()
    text = "The same long enough sentence appears in two separate source documents."
    items = [
        _item(1, _document("src_one", text)),
        _item(2, _document("src_two", text)),
    ]
    sentence_cache = _SentenceCache()

    duplicate_signals = agent._detect_duplicate_signals(
        items,
        sentence_cache=sentence_cache,
    )

    assert duplicate_signals[0].source_item_ids == ["src_one", "src_two"]
    assert len(sentence_cache._documents) == 2


@pytest.mark.asyncio
async def test_aggregation_output_keeps_failed_source_coverage_and_stored_source_ids() -> None:
    repo = AsyncMock()
    agent = MultiSourceAggregationAgent(aggregation_session_repo=repo)
    successful = _item(
        0,
        _document(
            "src_ok",
            "Successful source states revenue reached 42 million dollars this quarter.",
        ),
    )
    failed = SourceExtractionItemResult(
        position=1,
        item_id=101,
        source_item_id="src_failed",
        source_kind=SourceKind.WEB_ARTICLE,
        status=AggregationItemStatus.FAILED.value,
    )

    result = await agent.execute(
        MultiSourceAggregationInput(
            session_id=55,
            correlation_id="cid-source-coverage",
            items=[successful, failed],
            language="en",
        )
    )

    assert result.success is True
    assert result.output is not None
    assert {
        claim_source for claim in result.output.key_claims for claim_source in claim.source_item_ids
    } == {"src_ok"}
    assert [entry.source_item_id for entry in result.output.source_coverage] == [
        "src_ok",
        "src_failed",
    ]
    assert [entry.status for entry in result.output.source_coverage] == ["extracted", "failed"]
    assert result.output.source_coverage[1].used_in_summary is False
    repo.async_update_aggregation_session_output.assert_awaited_once()
