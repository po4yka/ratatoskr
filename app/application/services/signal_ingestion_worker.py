"""Continuous signal ingestion worker."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.application.services.signal_scoring import (
    SignalCandidate,
    SignalScoringService,
    VectorStoreUnavailableError,
)
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from datetime import datetime

    from app.application.ports.signal_sources import SignalSourceRepositoryPort

logger = get_logger(__name__)


@dataclass(slots=True, frozen=True)
class SignalIngestionStats:
    candidates: int
    persisted: int
    errors: int
    disabled: bool = False

    def to_dict(self) -> dict[str, int | bool]:
        return {
            "candidates": self.candidates,
            "persisted": self.persisted,
            "errors": self.errors,
            "disabled": self.disabled,
        }


class SignalIngestionWorker:
    """Load unscored feed items, score them, and persist user signal rows."""

    def __init__(
        self,
        *,
        repository: SignalSourceRepositoryPort,
        scorer: SignalScoringService,
        judge: Any | None = None,
    ) -> None:
        self._repository = repository
        self._scorer = scorer
        self._judge = judge

    async def run_once(
        self,
        *,
        limit: int = 100,
        now: datetime | None = None,
    ) -> dict[str, int | bool]:
        rows = await self._repository.async_list_unscored_candidates(limit=limit)
        candidates = [self._candidate_from_row(row) for row in rows]
        try:
            scored = await self._scorer.score(candidates, now=now)
        except VectorStoreUnavailableError:
            logger.warning("signal_ingestion_disabled_vector_unavailable")
            return SignalIngestionStats(
                candidates=len(candidates),
                persisted=0,
                errors=0,
                disabled=True,
            ).to_dict()

        rows_by_item_id = {int(row["feed_item_id"]): row for row in rows}
        judge_decisions = {}
        if self._judge is not None:
            judge_decisions = await self._judge.judge(scored, rows_by_item_id=rows_by_item_id)

        scored_by_item = {item.feed_item_id: item for item in scored}
        signal_records: list[dict[str, Any]] = []
        persisted = 0
        errors = 0
        for row in rows:
            score = scored_by_item.get(int(row["feed_item_id"]))
            if score is None:
                continue
            try:
                decision = judge_decisions.get(int(row["feed_item_id"]))
                signal_records.append(self._signal_record_from_score(row, score, decision))
            except Exception:
                errors += 1
                logger.warning(
                    "signal_ingestion_record_build_failed",
                    extra={
                        "user_id": row.get("user_id"),
                        "feed_item_id": row.get("feed_item_id"),
                    },
                    exc_info=True,
                )

        if signal_records:
            try:
                await self._repository.async_record_user_signals(signals=signal_records)
                persisted = len(signal_records)
            except Exception:
                for record in signal_records:
                    try:
                        await self._repository.async_record_user_signal(**record)
                        persisted += 1
                    except Exception:
                        errors += 1
                        logger.warning(
                            "signal_ingestion_persist_failed",
                            extra={
                                "user_id": record.get("user_id"),
                                "feed_item_id": record.get("feed_item_id"),
                            },
                            exc_info=True,
                        )

        return SignalIngestionStats(
            candidates=len(candidates),
            persisted=persisted,
            errors=errors,
        ).to_dict()

    @staticmethod
    def _candidate_from_row(row: dict[str, Any]) -> SignalCandidate:
        return SignalCandidate(
            feed_item_id=int(row["feed_item_id"]),
            source_id=int(row["source_id"]),
            source_kind=str(row["source_kind"]),
            title=row.get("title"),
            canonical_url=row.get("canonical_url"),
            published_at=row.get("published_at"),
            views=row.get("views"),
            forwards=row.get("forwards"),
            comments=row.get("comments"),
            metadata={"content_text": row.get("content_text")},
        )

    @staticmethod
    def _signal_record_from_score(
        row: dict[str, Any],
        score: Any,
        decision: Any | None,
    ) -> dict[str, Any]:
        status = "candidate"
        llm_score = None
        llm_judge = None
        llm_cost_usd = None
        filter_stage = "heuristic"
        final_score = score.score
        evidence = dict(score.evidence)
        if decision is not None:
            status = "queued" if decision.decision == "queue" else "dismissed"
            llm_score = decision.llm_score
            llm_judge = decision.evidence()
            llm_cost_usd = decision.cost_usd
            filter_stage = "llm_judge"
            final_score = decision.llm_score
            evidence["llm_judge"] = llm_judge
        return {
            "user_id": int(row["user_id"]),
            "feed_item_id": int(row["feed_item_id"]),
            "status": status,
            "heuristic_score": score.score,
            "llm_score": llm_score,
            "final_score": final_score,
            "evidence": evidence,
            "filter_stage": filter_stage,
            "llm_judge": llm_judge,
            "llm_cost_usd": llm_cost_usd,
        }
