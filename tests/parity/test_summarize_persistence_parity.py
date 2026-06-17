"""T9 persistence parity (ADR-0013): the graph's persist + terminal-failure paths
write the SAME rows the legacy summarize path would.

Postgres-backed; auto-skips without ``TEST_DATABASE_URL`` (via the shared
``database`` / ``session`` fixtures in tests/conftest.py). Marked ``contracts``.

What this proves (the legacy-deletion gate's persistence half):

* SUCCESS path -- the persist node, bound to REAL repositories, writes a
  ``summaries`` row whose payload == ``validate_and_shape_summary(canned)`` (the
  legacy oracle), AND a SUCCESS ``llm_calls`` row carrying
  ``attempt_trigger == 'graph_node'`` -- read straight off the column. This is the
  first write that ACTIVATES the reserved ``graph_node`` enum value end-to-end
  against a live Postgres enum constraint.
* FAILURE path -- a forced LLM failure routed through ``route_terminal_failure``
  persists a FAILURE ``llm_calls`` row carrying the REAL provider model
  (non-None, proving FIX-3) + the provider ``error_text`` (non-None), with
  ``attempt_trigger == 'graph_node'`` and ``status == 'error'``.
* No duplicate rows: exactly one summaries row and the expected llm_calls count.

Model strings / timestamps that are legitimately runtime-only are not asserted;
the payload + attempt_trigger + status + error_text are.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.application.graphs.summarize.deps import SummarizeConfig, SummarizeDeps
from app.application.graphs.summarize.lifecycle import route_terminal_failure
from app.application.graphs.summarize.nodes import persist, summarize
from app.core.summary_contract import validate_and_shape_summary
from app.db.models import LLMCall, Summary
from tests import db_helpers_async as dbh

pytestmark = pytest.mark.contracts


# The canned, contract-valid summary the graph would have produced. The persist
# node writes whatever is in state['summary']; for parity we put the legacy
# oracle's own output there so the persisted payload is unambiguously the legacy
# shape.
_CANNED_SUMMARY: dict[str, Any] = validate_and_shape_summary(
    {
        "summary_250": "A concise persisted summary.",
        "summary_1000": "A longer persisted summary covering the key points of the source.",
        "tldr": "Persisted gist.",
        "topic_tags": ["Persist", "persist"],
        "source_type": "article",
    }
)


def _real_deps(database: Any) -> SummarizeDeps:
    """Bind REAL persistence adapters to the test ``Database``; no-op the vector index.

    summary_index is a no-op so the persist node's read-your-writes fast-path
    doesn't require a live Qdrant -- it is best-effort and gated on user_scope /
    environment anyway, which we leave unset.
    """
    from app.infrastructure.persistence.repositories.crawl_result_repository import (
        CrawlResultRepositoryAdapter,
    )
    from app.infrastructure.persistence.repositories.llm_repository import LLMRepositoryAdapter
    from app.infrastructure.persistence.repositories.request_repository import (
        RequestRepositoryAdapter,
    )
    from app.infrastructure.persistence.repositories.summary_repository import (
        SummaryRepositoryAdapter,
    )

    noop = SimpleNamespace(index_summary=AsyncMock())
    return SummarizeDeps(
        llm_client=SimpleNamespace(),
        retrieval=SimpleNamespace(),
        extraction=SimpleNamespace(),
        stream_sink=SimpleNamespace(),
        summaries=SummaryRepositoryAdapter(database),
        requests=RequestRepositoryAdapter(database),
        summary_index=noop,
        llm_repo=LLMRepositoryAdapter(database),
        crawl_repo=CrawlResultRepositoryAdapter(database),
        config=SummarizeConfig(
            model="base-model",
            temperature=0.2,
            structured_output_mode="json_schema",
            long_context_threshold_tokens=1_000_000,
        ),
    )


async def _seed_request(session: Any, correlation_id: str) -> int:
    """Seed a request row (db_helpers_async.create_request) and commit it."""
    request_id = await dbh.create_request(
        session,
        type_="url",
        status="processing",
        correlation_id=correlation_id,
        input_url="https://example.com/article",
        normalized_url="https://example.com/article",
    )
    await session.commit()
    return request_id


async def _count(session: Any, model: Any, request_id: int) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(model).where(model.request_id == request_id)
        )
        or 0
    )


# =========================================================================== #
# SUCCESS path -- summaries payload + SUCCESS graph_node llm_calls row.
# =========================================================================== #


async def test_persist_writes_summary_and_graph_node_llm_call(database: Any, session: Any) -> None:
    """persist node writes legacy-shaped summary + a SUCCESS attempt_trigger='graph_node' row."""
    request_id = await _seed_request(session, "cid-persist-success")
    deps = _real_deps(database)

    # A SUCCESS llm_calls record exactly as the summarize node would have emitted
    # (attempt_trigger='graph_node'); persist drains state['llm_calls'].
    llm_record = {
        "request_id": request_id,
        "provider": "openrouter",
        "model": "deepseek/deepseek-v4-flash",
        "tokens_prompt": 10,
        "tokens_completion": 5,
        "cost_usd": None,
        "latency_ms": 123,
        "status": "ok",
        "structured_output_used": True,
        "structured_output_mode": "json_schema",
        "attempt_trigger": "graph_node",
    }
    state: dict[str, Any] = {
        "correlation_id": "cid-persist-success",
        "request_id": request_id,
        "lang": "en",
        "summary": dict(_CANNED_SUMMARY),
        "llm_calls": [llm_record],
    }

    out = await persist(state, deps=deps)
    assert "summary_id" in out  # id resolved from the just-written row

    # Re-read in a fresh transaction so we see committed rows.
    async with database.session() as read:
        summary_row = await read.scalar(select(Summary).where(Summary.request_id == request_id))
        assert summary_row is not None
        # The persisted payload IS the legacy oracle's shaped summary.
        assert summary_row.json_payload == _CANNED_SUMMARY

        llm_rows = (
            (await read.execute(select(LLMCall).where(LLMCall.request_id == request_id)))
            .scalars()
            .all()
        )
        assert len(llm_rows) == 1
        row = llm_rows[0]
        # Reading the column directly ACTIVATES the reserved enum value end-to-end.
        assert row.attempt_trigger == "graph_node"
        assert row.status == "ok"
        assert row.model == "deepseek/deepseek-v4-flash"

        # No duplicate summaries row.
        assert await _count(read, Summary, request_id) == 1


# =========================================================================== #
# FAILURE path -- a forced LLM failure persists a FAILURE graph_node llm_calls
# row carrying the REAL model (FIX-3) + provider error_text.
# =========================================================================== #


async def test_terminal_failure_persists_failure_llm_call_with_real_model(
    database: Any, session: Any
) -> None:
    """A forced LLM failure -> FAILURE llm_calls row: real model + error_text, status=error."""
    request_id = await _seed_request(session, "cid-persist-failure")
    deps = _real_deps(database)

    # Drive the REAL summarize node with a client that raises, attaching a raw
    # provider result (FIX-3: _tag_failure reads __llm_result__ for real model /
    # error_text / latency). This is the same failure record the live graph emits.
    raw_result = SimpleNamespace(
        model="x-ai/grok-4.20-beta",
        model_used=None,
        error_text="provider rate-limited (429)",
        tokens_prompt=7,
        tokens_completion=0,
        cost_usd=None,
        latency_ms=1234,
    )
    inner = RuntimeError("boom")
    inner.__llm_result__ = raw_result  # type: ignore[attr-defined]

    failing_deps = SummarizeDeps(
        llm_client=SimpleNamespace(chat_structured=AsyncMock(side_effect=inner)),
        retrieval=deps.retrieval,
        extraction=deps.extraction,
        stream_sink=deps.stream_sink,
        summaries=deps.summaries,
        requests=deps.requests,
        summary_index=deps.summary_index,
        llm_repo=deps.llm_repo,
        crawl_repo=deps.crawl_repo,
        config=deps.config,
    )

    state: dict[str, Any] = {
        "correlation_id": "cid-persist-failure",
        "request_id": request_id,
        "lang": "en",
        "messages": [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
        ],
        "content_for_summary": "the source",
    }

    with pytest.raises(ValueError) as exc_info:
        await summarize(state, deps=failing_deps)

    # The node tags the exception with the failure record; route it to the single
    # terminal sink, which drains the record into llm_calls (persist-everything).
    tagged = exc_info.value
    message = await route_terminal_failure(state, failing_deps, tagged)
    assert "Error ID" in message and "cid-persist-failure" in message

    async with database.session() as read:
        llm_rows = (
            (await read.execute(select(LLMCall).where(LLMCall.request_id == request_id)))
            .scalars()
            .all()
        )
        assert len(llm_rows) == 1, "exactly one failure row -- no duplicates"
        row = llm_rows[0]
        assert row.attempt_trigger == "graph_node"
        assert row.status == "error"
        # FIX-3: real provider model (never None per rule 11) + provider error_text.
        assert row.model == "x-ai/grok-4.20-beta"
        assert row.error_text and "rate-limited" in row.error_text

        # No summary row was written on the failure path.
        assert await _count(read, Summary, request_id) == 0


async def test_failure_record_falls_back_to_config_model_when_no_raw_result(
    database: Any, session: Any
) -> None:
    """FIX-3: with no __llm_result__, the failure row falls back to the config model.

    Proves the row's ``model`` is NEVER None (rule 11): even a bare timeout before
    the first byte persists a model-queryable row.
    """
    request_id = await _seed_request(session, "cid-persist-failure-2")
    deps = _real_deps(database)

    failing_deps = SummarizeDeps(
        llm_client=SimpleNamespace(
            chat_structured=AsyncMock(side_effect=RuntimeError("timeout before first byte"))
        ),
        retrieval=deps.retrieval,
        extraction=deps.extraction,
        stream_sink=deps.stream_sink,
        summaries=deps.summaries,
        requests=deps.requests,
        summary_index=deps.summary_index,
        llm_repo=deps.llm_repo,
        crawl_repo=deps.crawl_repo,
        config=deps.config,
    )
    state: dict[str, Any] = {
        "correlation_id": "cid-persist-failure-2",
        "request_id": request_id,
        "lang": "en",
        "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        "content_for_summary": "the source",
    }
    with pytest.raises(ValueError) as exc_info:
        await summarize(state, deps=failing_deps)
    await route_terminal_failure(state, failing_deps, exc_info.value)

    async with database.session() as read:
        row = await read.scalar(select(LLMCall).where(LLMCall.request_id == request_id))
        assert row is not None
        assert row.attempt_trigger == "graph_node"
        assert row.status == "error"
        # Config model fallback (deps.config.model) -- never None.
        assert row.model == "base-model"
        assert row.error_text is not None
