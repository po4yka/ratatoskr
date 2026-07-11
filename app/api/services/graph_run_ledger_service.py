"""Read-only, privacy-safe graph-run ledger and evaluation projections."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.api.exceptions import ResourceNotFoundError
from app.api.models.responses.graph_run_ledger import (
    GraphRunEvaluationListResponse,
    GraphRunLedgerAttempt,
    GraphRunLedgerChronologyEntry,
    GraphRunLedgerFeedback,
    GraphRunLedgerMetrics,
    GraphRunLedgerResponse,
)
from app.db.models import LLMCall, ProgressEvent, Request, Summary, SummaryFeedback

if TYPE_CHECKING:
    from collections.abc import Iterable

    from app.db.session import Database


_SAFE_LABEL = re.compile(r"[^a-zA-Z0-9_.:/@+-]+")
_MAX_LABEL_LENGTH = 128


class GraphRunLedgerService:
    """Build owner-only evaluation records without loading sensitive payload columns."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def get_run(self, *, request_id: int) -> GraphRunLedgerResponse:
        ledgers = await self._load_ledgers(request_ids=[request_id])
        if not ledgers:
            raise ResourceNotFoundError("Request", request_id)
        return ledgers[0]

    async def list_evaluations(self, *, limit: int) -> GraphRunEvaluationListResponse:
        async with self._database.session() as session:
            request_ids = list(
                (
                    await session.scalars(
                        select(Request.id)
                        .join(Summary, Summary.request_id == Request.id)
                        .where(Request.is_deleted.is_(False), Summary.is_deleted.is_(False))
                        .order_by(Request.created_at.desc())
                        .limit(limit)
                    )
                ).all()
            )
        return GraphRunEvaluationListResponse(
            items=await self._load_ledgers(request_ids=request_ids), limit=limit
        )

    async def _load_ledgers(self, *, request_ids: list[int]) -> list[GraphRunLedgerResponse]:
        if not request_ids:
            return []
        async with self._database.session() as session:
            requests = list(
                (
                    await session.scalars(
                        select(Request).where(
                            Request.id.in_(request_ids), Request.is_deleted.is_(False)
                        )
                    )
                ).all()
            )
            if not requests:
                return []
            actual_ids = [request.id for request in requests]
            events = list(
                (
                    await session.scalars(
                        select(ProgressEvent)
                        .where(ProgressEvent.request_id.in_(actual_ids))
                        .order_by(ProgressEvent.request_id, ProgressEvent.sequence)
                    )
                ).all()
            )
            calls = list(
                (
                    await session.scalars(
                        select(LLMCall)
                        .where(LLMCall.request_id.in_(actual_ids), LLMCall.is_deleted.is_(False))
                        .order_by(LLMCall.request_id, LLMCall.attempt_index)
                    )
                ).all()
            )
            summaries = list(
                (
                    await session.scalars(
                        select(Summary).where(
                            Summary.request_id.in_(actual_ids), Summary.is_deleted.is_(False)
                        )
                    )
                ).all()
            )
            feedback = list(
                (
                    await session.scalars(
                        select(SummaryFeedback).where(
                            SummaryFeedback.summary_id.in_([summary.id for summary in summaries])
                        )
                    )
                ).all()
                if summaries
                else []
            )

        events_by_request: dict[int, list[Any]] = defaultdict(list)
        calls_by_request: dict[int, list[Any]] = defaultdict(list)
        feedback_by_summary: dict[int, list[Any]] = defaultdict(list)
        summary_by_request = {summary.request_id: summary.id for summary in summaries}
        for event in events:
            events_by_request[event.request_id].append(event)
        for call in calls:
            calls_by_request[call.request_id].append(call)
        for item in feedback:
            feedback_by_summary[item.summary_id].append(item)

        ledgers_by_id = {
            request.id: build_graph_run_ledger(
                request=request,
                events=events_by_request[request.id],
                calls=calls_by_request[request.id],
                feedback=feedback_by_summary[summary_by_request[request.id]]
                if request.id in summary_by_request
                else [],
            )
            for request in requests
        }
        return [
            ledgers_by_id[request_id] for request_id in request_ids if request_id in ledgers_by_id
        ]


def build_graph_run_ledger(
    *, request: Any, events: Iterable[Any], calls: Iterable[Any], feedback: Iterable[Any]
) -> GraphRunLedgerResponse:
    """Project persisted run data onto a strict allow-list of safe fields."""
    chronology = [
        GraphRunLedgerChronologyEntry(
            sequence=int(event.sequence),
            kind=_safe_label(event.kind),
            stage=_safe_optional_label(event.stage),
            status=_safe_optional_label(event.status),
            occurred_at=event.created_at,
        )
        for event in events
    ]
    attempts = [_attempt_from_call(call) for call in calls]
    feedback_items = list(feedback)
    metrics = GraphRunLedgerMetrics(
        node_count=len(chronology),
        attempt_count=len(attempts),
        repair_count=sum(attempt.trigger == "repair_loop" for attempt in attempts),
        fallback_count=sum(attempt.fallback_model is not None for attempt in attempts),
        graph_latency_ms=_safe_nonnegative_int(getattr(request, "processing_time_ms", None)),
        llm_latency_ms=sum(attempt.latency_ms or 0 for attempt in attempts),
        prompt_tokens=sum(attempt.prompt_tokens or 0 for attempt in attempts),
        completion_tokens=sum(attempt.completion_tokens or 0 for attempt in attempts),
        total_cost_usd=round(sum(attempt.cost_usd or 0.0 for attempt in attempts), 6),
    )
    return GraphRunLedgerResponse(
        request_id=int(request.id),
        request_status=_safe_label(request.status),
        created_at=request.created_at,
        chronology=chronology,
        attempts=attempts,
        metrics=metrics,
        feedback=_feedback_projection(feedback_items),
    )


def _attempt_from_call(call: Any) -> GraphRunLedgerAttempt:
    return GraphRunLedgerAttempt(
        attempt_index=int(call.attempt_index),
        trigger=_safe_label(call.attempt_trigger),
        provider=_safe_optional_label(call.provider),
        model=_safe_optional_label(call.model),
        status=_safe_optional_label(call.status),
        latency_ms=_safe_nonnegative_int(call.latency_ms),
        total_latency_ms=_safe_nonnegative_int(call.total_latency_ms),
        prompt_tokens=_safe_nonnegative_int(call.tokens_prompt),
        completion_tokens=_safe_nonnegative_int(call.tokens_completion),
        cost_usd=_safe_nonnegative_float(call.cost_usd),
        fallback_model=_safe_optional_label(call.fallback_model_used),
        retry_exhausted=bool(call.retry_exhausted),
        error_present=bool(call.error_text),
    )


def _feedback_projection(items: list[Any]) -> GraphRunLedgerFeedback:
    ratings = [int(item.rating) for item in items if item.rating is not None]
    timestamps = [item.updated_at for item in items if item.updated_at is not None]
    return GraphRunLedgerFeedback(
        feedback_count=len(items),
        rating_average=round(sum(ratings) / len(ratings), 2) if ratings else None,
        issue_count=sum(_issue_count(item.issues) for item in items),
        latest_feedback_at=max(timestamps) if timestamps else None,
    )


def _issue_count(value: object) -> int:
    if not isinstance(value, str):
        return 0
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return 0
    if not isinstance(parsed, list):
        return 0
    return sum(isinstance(item, str) and bool(item) for item in parsed)


def _safe_optional_label(value: object) -> str | None:
    return _safe_label(value) if value else None


def _safe_label(value: object) -> str:
    normalized = _SAFE_LABEL.sub("_", str(getattr(value, "value", value))).strip("_")
    return normalized[:_MAX_LABEL_LENGTH] or "unknown"


def _safe_nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if not isinstance(value, str):
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _safe_nonnegative_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else None
    if not isinstance(value, str):
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None
