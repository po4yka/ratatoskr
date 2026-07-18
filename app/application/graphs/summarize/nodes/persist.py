"""``persist`` node -- write summaries + llm_calls + read-your-writes index (ADR-0011/0012/0015)."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from app.application.dto.vector_search import RetrievalScope
from app.application.graphs.summarize.nodes._span import graph_node
from app.application.services.summarization.metadata_backfill import backfill_summary_metadata
from app.domain.models.request import RequestStatus

if TYPE_CHECKING:
    from app.application.graphs.summarize.deps import SummarizeDeps
    from app.application.graphs.summarize.state import SummarizeState

logger = logging.getLogger(__name__)


@graph_node("persist")
async def persist(state: SummarizeState, *, deps: SummarizeDeps) -> dict[str, Any]:
    """Persist the summary, the llm_calls, and finalize the request.

    persist-everything (ADR-0011): atomically writes the ``summaries`` row and
    every accumulated ``llm_calls`` record, then attempts the downstream index
    and export effects before flipping the request to COMPLETED. A DB failure or
    process stop therefore leaves the request resumable instead of terminal with
    a missing audit trail.

    Then T6's read-your-writes vector fast-path (ADR-0012): once the summary row
    exists -- and BEFORE the request is considered done by downstream pollers --
    the summary is indexed into Qdrant synchronously so a subsequent request's
    ``ground`` node sees it without waiting for the next reconciler pass. Best-effort:
    a vector-store failure is logged (with ``correlation_id``) and left to the
    reconciler; the summary row is the source of truth and completion is never
    blocked. Not gated by ``SUMMARIZE_RAG_ENABLED`` -- every persisted summary
    should be queryable (search/MCP/grounding), independent of RAG grounding.

    GAP 4 (metadata backfill): when ``deps.crawl_repo`` is set, calls
    :func:`~app.application.services.summarization.metadata_backfill.backfill_summary_metadata`
    best-effort before writing the summary row, so ``canonical_url`` / ``domain`` /
    ``title`` / ``author`` / date fields from the crawl result and request URL are
    populated. The LLM-completion and RAG-enrichment sub-steps are deferred (see
    metadata_backfill module docstring).

    GAP 2 fix (Redis cache-poisoning): the ``summary_cache`` write happens here,
    not in ``summarize``, because ``persist`` only runs after ``validate`` (and
    the optional ``repair`` loop) has confirmed the summary against the contract
    -- the ``validate`` -> ``repair`` route never reaches ``persist``. Writing
    the cache immediately after the LLM call (the old location) let a
    malformed-but-truthy response poison the shared, content-hash-keyed cache for
    every subsequent request to that URL, with ``repair`` never evicting it. The
    write runs before the ``request_id is None`` short-circuit below (cache
    lookup/write is not gated on a request row existing) but is otherwise
    best-effort and never blocks completion.
    """
    summary = state.get("summary") or {}
    request_id = state.get("request_id")

    await _write_summary_cache(state, deps, summary)

    # ``request_id is None`` is the content-only path (no request row): short-circuit
    # ALL DB writes -- finalize, llm_calls, and the index fast-path. INSERTing a
    # Summary / LLMCall against a non-existent ``requests.id`` (None, or the old ``0``
    # sentinel) raises ForeignKeyViolationError that the facade silently swallows to
    # ``{}``. The summary still returns to the caller; it is just never persisted here.
    if not summary or request_id is None:
        return {}

    # GAP 4: backfill missing metadata from crawl/request before persisting.
    if deps.crawl_repo is not None:
        try:
            summary = await backfill_summary_metadata(
                summary,
                request_id=request_id,
                content_text=state.get("content_for_summary") or state.get("source_text") or "",
                correlation_id=state.get("correlation_id"),
                request_repo=deps.requests,
                crawl_repo=deps.crawl_repo,
            )
        except Exception:
            logger.warning(
                "graph_persist_metadata_backfill_failed",
                extra={
                    "correlation_id": state.get("correlation_id"),
                    "request_id": request_id,
                },
                exc_info=True,
            )

    lang = state.get("lang") or "en"
    insights = summary.get("insights") if isinstance(summary.get("insights"), dict) else None

    # The UPSERT returns the summary id (and version) directly, so there is no
    # follow-up async_get_summary_id_by_request round-trip.
    llm_calls = _llm_calls_with_attempt_indices(state)
    finalize_result = await deps.summaries.async_persist_summary_with_llm_calls(
        request_id=request_id,
        lang=lang,
        json_payload=summary,
        llm_calls=llm_calls,
        insights_json=insights,
        is_read=False,
    )
    summary_id = finalize_result.summary_id

    # Both downstream effects see the committed summary. They are independently
    # best-effort, but cancellation/process death propagates and leaves the request
    # non-terminal so checkpoint recovery can retry the persist node.
    await asyncio.gather(
        _index_summary_for_freshness(state, deps, summary_id=summary_id),
        _publish_summary_created(state, deps, summary_id=summary_id),
    )
    await deps.requests.async_update_request_status(request_id, RequestStatus.COMPLETED)

    return {"summary_id": summary_id} if summary_id is not None else {}


def _llm_calls_with_attempt_indices(state: SummarizeState) -> list[dict[str, Any]]:
    """Give checkpointed graph calls deterministic, resume-idempotent indices."""
    request_id = state.get("request_id")
    records: list[dict[str, Any]] = []
    for attempt_index, record in enumerate(state.get("llm_calls") or [], start=1):
        normalized = dict(record)
        normalized["request_id"] = request_id
        normalized["attempt_index"] = attempt_index
        records.append(normalized)
    return records


async def _write_summary_cache(
    state: SummarizeState, deps: SummarizeDeps, summary: dict[str, Any]
) -> None:
    """Best-effort write of the validated summary to the Redis cache (GAP 2 fix).

    Only writes when: the streaming mode flag is off (streaming is a live-UX path
    and never touches the cache, mirroring the read-side check in ``summarize``,
    ADR-0017); a ``dedupe_hash`` and ``deps.summary_cache`` are both available; the
    summary is non-empty; and no ``validation_errors`` are recorded on state.
    The ``validation_errors`` check is defense-in-depth -- the graph topology
    already guarantees ``validate`` succeeded (routed to ``enrich``, not
    ``repair``) before ``persist`` ever runs -- so a malformed summary can never
    reach the shared, content-hash-keyed cache. A cache-write failure is logged
    and swallowed; it must never block completion.
    """
    if state.get("stream"):
        return
    dedupe_hash = state.get("dedupe_hash") or ""
    if not summary or not dedupe_hash or deps.summary_cache is None:
        return
    if state.get("validation_errors"):
        return
    lang = state.get("lang") or "en"
    try:
        await deps.summary_cache.set(dedupe_hash, lang, summary)
    except Exception:  # best-effort: cache failures must never block completion
        logger.warning(
            "graph_persist_summary_cache_write_failed",
            extra={
                "correlation_id": state.get("correlation_id"),
                "request_id": state.get("request_id"),
            },
            exc_info=True,
        )


async def _index_summary_for_freshness(
    state: SummarizeState, deps: SummarizeDeps, *, summary_id: int | None
) -> None:
    """Synchronous index-on-write; swallow vector-store failures (ADR-0012)."""
    summary = state.get("summary") or {}
    request_id = state.get("request_id")
    user_scope = state.get("user_scope")
    environment = state.get("environment")
    if not summary or summary_id is None or request_id is None or not user_scope or not environment:
        # Nothing persisted yet or scope unavailable -- the reconciler still
        # converges later.
        return

    # Owner-wide summary point (no user_id in the payload -- matches the shared
    # point shape); user_scope + environment are the partition the index writes
    # + ground reads.
    scope = RetrievalScope(environment=environment, user_scope=user_scope, user_id=None)
    try:
        await deps.summary_index.index_summary(
            request_id=request_id,
            summary_id=summary_id,
            summary=summary,
            lang=state.get("lang"),
            scope=scope,
            correlation_id=state.get("correlation_id"),
        )
    except Exception:  # best-effort: freshness must never block completion (ADR-0012)
        logger.warning(
            "summary_index_fastpath_failed",
            extra={
                "correlation_id": state.get("correlation_id"),
                "request_id": request_id,
                "summary_id": summary_id,
            },
            exc_info=True,
        )


async def _publish_summary_created(
    state: SummarizeState, deps: SummarizeDeps, *, summary_id: int | None
) -> None:
    if deps.export_events is None or summary_id is None:
        return
    try:
        await deps.export_events.publish_summary_created(summary_id)
    except Exception:
        logger.warning(
            "summary_export_event_publish_failed",
            extra={"correlation_id": state.get("correlation_id"), "summary_id": summary_id},
            exc_info=True,
        )
