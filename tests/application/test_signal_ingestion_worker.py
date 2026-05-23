"""Tests for the continuous signal ingestion worker."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, cast

import pytest

from app.application.services.signal_ingestion_worker import SignalIngestionWorker
from app.application.services.signal_scoring import SignalCandidate, SignalScoringService
from app.core.time_utils import UTC

if TYPE_CHECKING:
    from app.application.ports.signal_sources import SignalSourceRepositoryPort


class _FakeTopicSimilarity:
    def is_ready(self) -> bool:
        return True

    async def score_item(self, candidate: SignalCandidate) -> float:
        return 0.9 if candidate.feed_item_id == 1 else 0.2


class _FakeSignalRepository:
    def __init__(self, *, fail_bulk: bool = False) -> None:
        self.recorded: list[dict] = []
        self.recorded_batches: list[list[dict]] = []
        self.fail_bulk = fail_bulk

    async def async_list_unscored_candidates(self, *, limit: int = 100) -> list[dict]:
        return [
            {
                "user_id": 1001,
                "feed_item_id": 1,
                "source_id": 10,
                "source_kind": "rss",
                "title": "Python post",
                "canonical_url": "https://example.com/1",
                "content_text": "Python content",
                "published_at": dt.datetime(2026, 4, 30, tzinfo=UTC),
                "views": 100,
                "forwards": 4,
                "comments": None,
            },
            {
                "user_id": 1001,
                "feed_item_id": 2,
                "source_id": 10,
                "source_kind": "rss",
                "title": "Other post",
                "canonical_url": "https://example.com/2",
                "content_text": "Other content",
                "published_at": dt.datetime(2026, 4, 30, tzinfo=UTC),
                "views": None,
                "forwards": None,
                "comments": None,
            },
        ]

    async def async_record_user_signal(self, **kwargs):
        self.recorded.append(dict(kwargs))
        return {"id": len(self.recorded), **kwargs}

    async def async_record_user_signals(self, *, signals):
        self.recorded_batches.append([dict(signal) for signal in signals])
        if self.fail_bulk:
            raise RuntimeError("bulk failed")
        return [await self.async_record_user_signal(**signal) for signal in signals]


@pytest.mark.asyncio
async def test_signal_ingestion_worker_scores_and_persists_candidates() -> None:
    repo = _FakeSignalRepository()
    worker = SignalIngestionWorker(
        repository=cast("SignalSourceRepositoryPort", repo),
        scorer=SignalScoringService(topic_similarity=_FakeTopicSimilarity()),
    )

    stats = await worker.run_once(limit=10, now=dt.datetime(2026, 4, 30, tzinfo=UTC))

    assert stats == {"candidates": 2, "persisted": 2, "errors": 0, "disabled": False}
    assert len(repo.recorded_batches) == 1
    assert [row["feed_item_id"] for row in repo.recorded] == [1, 2]
    assert repo.recorded[0]["status"] == "candidate"
    assert repo.recorded[0]["filter_stage"] == "heuristic"
    assert repo.recorded[0]["final_score"] > repo.recorded[1]["final_score"]


@pytest.mark.asyncio
async def test_signal_ingestion_worker_disables_when_scoring_is_not_ready() -> None:
    class NotReadySimilarity:
        def is_ready(self) -> bool:
            return False

        async def score_item(self, candidate: SignalCandidate) -> float:
            return 0.0

    repo = _FakeSignalRepository()
    worker = SignalIngestionWorker(
        repository=cast("SignalSourceRepositoryPort", repo),
        scorer=SignalScoringService(topic_similarity=NotReadySimilarity()),
    )

    stats = await worker.run_once()

    assert stats == {"candidates": 2, "persisted": 0, "errors": 0, "disabled": True}
    assert repo.recorded == []


@pytest.mark.asyncio
async def test_signal_ingestion_worker_applies_llm_judge_decisions() -> None:
    class Judge:
        async def judge(self, scored_candidates, *, rows_by_item_id):
            return {
                1: SimpleDecision(
                    llm_score=0.95,
                    decision="queue",
                    reason="important",
                    cost_usd=0.02,
                    latency_ms=50,
                    model="judge",
                )
            }

    class SimpleDecision:
        def __init__(self, **kwargs) -> None:
            self.__dict__.update(kwargs)

        def evidence(self):
            return {"reason": self.reason, "model": self.model}

    repo = _FakeSignalRepository()
    worker = SignalIngestionWorker(
        repository=cast("SignalSourceRepositoryPort", repo),
        scorer=SignalScoringService(topic_similarity=_FakeTopicSimilarity()),
        judge=Judge(),
    )

    stats = await worker.run_once(limit=10, now=dt.datetime(2026, 4, 30, tzinfo=UTC))

    assert stats["persisted"] == 2
    assert repo.recorded[0]["status"] == "queued"
    assert repo.recorded[0]["llm_score"] == 0.95
    assert repo.recorded[0]["llm_cost_usd"] == 0.02
    assert repo.recorded[0]["filter_stage"] == "llm_judge"


@pytest.mark.asyncio
async def test_signal_ingestion_worker_falls_back_when_bulk_persist_fails() -> None:
    repo = _FakeSignalRepository(fail_bulk=True)
    worker = SignalIngestionWorker(
        repository=cast("SignalSourceRepositoryPort", repo),
        scorer=SignalScoringService(topic_similarity=_FakeTopicSimilarity()),
    )

    stats = await worker.run_once(limit=10, now=dt.datetime(2026, 4, 30, tzinfo=UTC))

    assert stats == {"candidates": 2, "persisted": 2, "errors": 0, "disabled": False}
    assert len(repo.recorded_batches) == 1
    assert [row["feed_item_id"] for row in repo.recorded] == [1, 2]
