"""Facade-level tests for ``GraphURLProcessor`` (T9 cutover seam, ADR-0013).

These pin the legacy ``URLProcessor`` orchestration parity the facade must
preserve while delegating extraction->notify to the summarize graph. The graph
runners + collaborators are mocked; no langgraph / DB is required.

Scenarios:
  (a) cache hit short-circuits the graph (graph not invoked, cached result returned)
  (b) the synchronous crash-recovery lease is recorded on start AND outcome
  (c) post_summary_tasks scheduled only when not batch / not silent
  (d) interactive routes to the streamed runner with a sink; silent -> non-streamed
  (e) graph {"error": ...} -> URLProcessingFlowResult(success=False) + Error ID notify
  (f) content-only summarize returns the shaped dict with request quality metadata
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.adapters.content.graph_url_processor import GraphURLProcessor
from app.adapters.content.summarization_models import PureSummaryRequest
from app.adapters.content.url_flow_models import URLFlowRequest, URLProcessingFlowResult

pytestmark = pytest.mark.asyncio

# The plain runner (``run_summarize_graph``) is imported lazily from the
# application layer inside ``_run_graph`` -- patch the source module. The streamed
# runner is INJECTED into the facade (no app.di import from adapters), so it is
# swapped via the ``streamed_runner`` constructor arg captured in ``_RUNNERS``.
_PLAIN_PATH = "app.application.graphs.summarize.graph.run_summarize_graph"

# Mutable holder so ``_facade`` picks up the streamed runner ``_patch_runners`` set.
_RUNNERS: dict[str, Any] = {}


def _patch_runners(monkeypatch, *, streamed, plain) -> None:
    monkeypatch.setattr(_PLAIN_PATH, plain, raising=True)
    _RUNNERS["streamed"] = streamed


def _patch_lease(monkeypatch, repo=None):
    repo = repo or MagicMock(
        record_synchronous_start=AsyncMock(), record_synchronous_outcome=AsyncMock()
    )
    monkeypatch.setattr(
        "app.infrastructure.persistence.request_processing_job_repository."
        "RequestProcessingJobRepository",
        lambda *_a, **_k: repo,
    )
    return repo


def _cfg() -> Any:
    return SimpleNamespace(
        runtime=SimpleNamespace(
            preferred_lang="en",
            url_flow_lease_ttl_sec=900,
            llm_request_slow_threshold_sec=300.0,
            summary_prompt_version="v1",
        ),
        openrouter=SimpleNamespace(model="base-model", structured_output_mode="json_schema"),
        vector_store=SimpleNamespace(user_scope="owner", environment="prod"),
        redis=SimpleNamespace(llm_ttl_seconds=7_200),
    )


def _message_persistence(**over: Any) -> Any:
    """Persistence-facade stub: ``request_repo`` (create) + ``persist_message_snapshot``.

    Mirrors the production ``MessagePersistence`` surface the facade now uses so the
    request row carries its owner ``user_id`` AND a telegram_messages snapshot is
    written (persist-everything + IDOR rule 12).
    """
    mp = MagicMock(
        request_repo=MagicMock(async_create_request=AsyncMock(return_value=777)),
        persist_message_snapshot=AsyncMock(),
    )
    for key, val in over.items():
        setattr(mp, key, val)
    return mp


def _facade(**over: Any) -> GraphURLProcessor:
    defaults: dict[str, Any] = {
        "cfg": _cfg(),
        "db": MagicMock(),
        "graph": MagicMock(),
        "deps": MagicMock(),
        "stream_sink_factory": MagicMock(return_value=MagicMock()),
        "streamed_runner": _RUNNERS.get("streamed") or AsyncMock(),
        "cached_summary_responder": MagicMock(maybe_reply=AsyncMock(return_value=None)),
        "post_summary_tasks": MagicMock(schedule_tasks=AsyncMock(), aclose=AsyncMock()),
        "summary_delivery": MagicMock(
            deliver_summary=AsyncMock(return_value=URLProcessingFlowResult(success=True)),
            send_processing_failure=AsyncMock(return_value=URLProcessingFlowResult(success=False)),
            aclose=AsyncMock(),
        ),
        "response_formatter": MagicMock(
            send_error_notification=AsyncMock(),
            react=AsyncMock(),
            send_cover_message=AsyncMock(),
        ),
        "request_repo": MagicMock(
            async_create_request=AsyncMock(return_value=777),
            async_update_request_status=AsyncMock(),
        ),
        "message_persistence": _message_persistence(),
    }
    defaults.update(over)
    return GraphURLProcessor(**defaults)


def _url_request(**over: Any) -> URLFlowRequest:
    base: dict[str, Any] = {
        # Production-shaped message: ``from_user.id`` is the owner, ``chat.id`` the
        # chat, ``id`` the message id -- NOT sender/sender_id/chat_id.
        "message": SimpleNamespace(
            chat=SimpleNamespace(id=10, type="private", title=None, username=None),
            from_user=SimpleNamespace(id=30, username="owner"),
            id=20,
        ),
        "url_text": "https://example.com/article",
        "correlation_id": "cid-1",
    }
    base.update(over)
    return URLFlowRequest(**base)


_GOOD_SUMMARY = {"tldr": "t", "summary_250": "s", "summary_1000": "l"}


@pytest.fixture(autouse=True)
def _silence_side_effects(monkeypatch):
    """Neutralize the non-graph orchestration side-effects (span/metric/lease/typing)."""
    _RUNNERS.clear()
    # typing_indicator is an async context manager around the flow.
    import contextlib

    @contextlib.asynccontextmanager
    async def _noop_typing(*_a, **_k):
        yield

    monkeypatch.setattr("app.utils.typing_indicator.typing_indicator", _noop_typing, raising=True)
    # OTel span helpers.
    monkeypatch.setattr(
        "app.observability.otel.get_tracer",
        lambda *_a, **_k: SimpleNamespace(start_as_current_span=lambda *a, **k: _NoopCm()),
        raising=True,
    )
    monkeypatch.setattr(
        "app.observability.otel.set_correlation_id_attr", lambda *_a, **_k: None, raising=True
    )
    # Metrics.
    monkeypatch.setattr(
        "app.observability.metrics.set_url_processor_in_flight", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "app.observability.metrics.record_llm_request_total_latency", lambda *_a, **_k: None
    )


class _NoopCm:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_a) -> bool:
        return False


# --------------------------------------------------------------------------- #
# (a) cache hit short-circuits the graph
# --------------------------------------------------------------------------- #
async def test_cache_hit_short_circuits_graph(monkeypatch):
    cached = URLProcessingFlowResult.from_summary(_GOOD_SUMMARY, cached=True, request_id=5)
    responder = MagicMock(maybe_reply=AsyncMock(return_value=cached))
    run_streamed = AsyncMock()
    run_plain = AsyncMock()
    _patch_runners(monkeypatch, streamed=run_streamed, plain=run_plain)

    facade = _facade(cached_summary_responder=responder)
    result = await facade.handle_url_flow(_url_request())

    assert result is cached
    assert result.cached is True
    run_streamed.assert_not_awaited()
    run_plain.assert_not_awaited()
    facade.message_persistence.request_repo.async_create_request.assert_not_awaited()


# --------------------------------------------------------------------------- #
# (b) lease record_synchronous_start + record_synchronous_outcome both called
# --------------------------------------------------------------------------- #
async def test_lease_start_and_outcome_recorded(monkeypatch):
    lease_repo = _patch_lease(monkeypatch)
    _patch_runners(
        monkeypatch,
        streamed=AsyncMock(return_value={"summary": _GOOD_SUMMARY, "source_text": "body"}),
        plain=AsyncMock(),
    )

    facade = _facade()
    await facade.handle_url_flow(_url_request())

    lease_repo.record_synchronous_start.assert_awaited_once()
    lease_repo.record_synchronous_outcome.assert_awaited_once()
    _, kwargs = lease_repo.record_synchronous_outcome.call_args
    assert kwargs["status"] == "succeeded"
    assert kwargs["request_id"] == 777


# --------------------------------------------------------------------------- #
# (c) post_summary_tasks scheduled only when not batch / not silent
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("silent", "batch", "expected"),
    [(False, False, True), (False, True, False), (True, False, True)],
)
async def test_post_summary_tasks_gating(monkeypatch, silent, batch, expected):
    _patch_lease(monkeypatch)
    _patch_runners(
        monkeypatch,
        streamed=AsyncMock(
            return_value={
                "summary": _GOOD_SUMMARY,
                "source_text": "body",
                "dedupe_hash": "canonical-hash",
            }
        ),
        plain=AsyncMock(
            return_value={
                "summary": _GOOD_SUMMARY,
                "source_text": "body",
                "dedupe_hash": "canonical-hash",
            }
        ),
    )

    facade = _facade()
    await facade.handle_url_flow(_url_request(silent=silent, batch_mode=batch))

    assert facade.post_summary_tasks.schedule_tasks.await_count == (1 if expected else 0)
    if expected:
        _, kwargs = facade.post_summary_tasks.schedule_tasks.await_args
        assert kwargs["url_hash"] == "canonical-hash"


# --------------------------------------------------------------------------- #
# (d) interactive -> streamed runner (with sink); silent -> non-streamed runner
# --------------------------------------------------------------------------- #
async def test_interactive_routes_to_streamed_runner(monkeypatch):
    _patch_lease(monkeypatch)
    streamed = AsyncMock(return_value={"summary": _GOOD_SUMMARY, "source_text": "body"})
    plain = AsyncMock(return_value={"summary": _GOOD_SUMMARY, "source_text": "body"})
    _patch_runners(monkeypatch, streamed=streamed, plain=plain)

    sink = MagicMock()
    facade = _facade(stream_sink_factory=MagicMock(return_value=sink))
    await facade.handle_url_flow(_url_request(silent=False, batch_mode=False))

    streamed.assert_awaited_once()
    plain.assert_not_awaited()
    _, kwargs = streamed.call_args
    assert kwargs["sink"] is sink


async def test_silent_routes_to_non_streamed_runner(monkeypatch):
    _patch_lease(monkeypatch)
    streamed = AsyncMock(return_value={"summary": _GOOD_SUMMARY, "source_text": "body"})
    plain = AsyncMock(return_value={"summary": _GOOD_SUMMARY, "source_text": "body"})
    _patch_runners(monkeypatch, streamed=streamed, plain=plain)

    facade = _facade()
    await facade.handle_url_flow(_url_request(silent=True))

    plain.assert_awaited_once()
    streamed.assert_not_awaited()


async def test_interactive_uses_plain_runner_when_streaming_flag_disabled(monkeypatch):
    """audit #19: SUMMARY_STREAMING_ENABLED=False makes even interactive URL
    summaries take the plain (non-streamed) runner."""
    _patch_lease(monkeypatch)
    streamed = AsyncMock(return_value={"summary": _GOOD_SUMMARY, "source_text": "body"})
    plain = AsyncMock(return_value={"summary": _GOOD_SUMMARY, "source_text": "body"})
    _patch_runners(monkeypatch, streamed=streamed, plain=plain)

    cfg = _cfg()
    cfg.runtime.summary_streaming_enabled = False
    facade = _facade(cfg=cfg)
    await facade.handle_url_flow(_url_request(silent=False, batch_mode=False))

    plain.assert_awaited_once()
    streamed.assert_not_awaited()


async def test_interactive_streams_when_flag_enabled(monkeypatch):
    """The flag defaults on: an interactive request still streams when True."""
    _patch_lease(monkeypatch)
    streamed = AsyncMock(return_value={"summary": _GOOD_SUMMARY, "source_text": "body"})
    plain = AsyncMock(return_value={"summary": _GOOD_SUMMARY, "source_text": "body"})
    _patch_runners(monkeypatch, streamed=streamed, plain=plain)

    cfg = _cfg()
    cfg.runtime.summary_streaming_enabled = True
    facade = _facade(cfg=cfg)
    await facade.handle_url_flow(_url_request(silent=False, batch_mode=False))

    streamed.assert_awaited_once()
    plain.assert_not_awaited()


# --------------------------------------------------------------------------- #
# (e) graph {"error": ...} -> failure result + Error ID notification
# --------------------------------------------------------------------------- #
async def test_graph_terminal_error_returns_failure_and_notifies(monkeypatch):
    _patch_lease(monkeypatch)
    _patch_runners(
        monkeypatch,
        streamed=AsyncMock(
            return_value={
                "error": "Processing failed (Error ID: cid-1). Please try again.",
                "correlation_id": "cid-1",
                "request_id": 777,
            }
        ),
        plain=AsyncMock(),
    )

    facade = _facade()
    result = await facade.handle_url_flow(_url_request())

    assert result.success is False
    assert result.request_id == 777
    facade.summary_delivery.send_processing_failure.assert_awaited_once()
    facade.post_summary_tasks.schedule_tasks.assert_not_awaited()


# --------------------------------------------------------------------------- #
# (f) content-only summarize returns shaped dict with quality metadata
# --------------------------------------------------------------------------- #
async def test_content_only_summarize_applies_quality_metadata():
    graph = MagicMock(ainvoke=AsyncMock(return_value={"summary": dict(_GOOD_SUMMARY)}))
    facade = _facade(graph=graph)

    out = await facade.summarize(
        PureSummaryRequest(
            content_text="pre-extracted body",
            chosen_lang="en",
            system_prompt="sys",
            correlation_id="cid-2",
            source_coverage="full",
            extraction_quality="high",
            extraction_confidence=0.9,
        )
    )

    graph.ainvoke.assert_awaited_once()
    # The extract node is skipped: input_url empty, source_text carries the content.
    init_state = graph.ainvoke.call_args.args[0]
    assert init_state["input_url"] == ""
    assert init_state["source_text"] == "pre-extracted body"
    # Quality metadata the graph nodes do NOT set is applied by the facade
    # (merge_summary_quality_metadata writes the ``summary_quality`` key).
    quality = out.get("summary_quality")
    assert isinstance(quality, dict)
    assert quality.get("source_coverage") == "full"
    assert quality.get("extraction_quality") == "high"
    assert quality.get("extraction_confidence") == 0.9


async def test_content_only_summarize_drives_real_persist_node_no_db_writes(monkeypatch):
    """audit #1: facade.summarize() must pass request_id=None so the REAL persist
    node short-circuits every DB write (no FK violation against requests.id=0).

    Drives the real ``persist`` node (no langgraph): the graph stub's ``ainvoke``
    invokes ``persist`` against the facade-built initial state (with a summary
    grafted on, mimicking the spine), then returns the final state. Asserts the
    persist node NEVER awaits async_finalize_request_summary / async_insert_llm_call /
    index_summary (zero DB writes), and the facade still returns a shaped dict.
    """
    from app.application.graphs.summarize.deps import SummarizeDeps
    from app.application.graphs.summarize.nodes import persist as persist_node_fn

    finalize = AsyncMock()
    insert_llm = AsyncMock()
    index = AsyncMock()
    persist_deps = SummarizeDeps(
        llm_client=MagicMock(),
        retrieval=MagicMock(),
        extraction=MagicMock(),
        stream_sink=MagicMock(),
        summaries=MagicMock(
            async_finalize_request_summary=finalize,
            async_get_summary_id_by_request=AsyncMock(return_value=123),
        ),
        requests=MagicMock(),
        summary_index=MagicMock(index_summary=index),
        llm_repo=MagicMock(async_insert_llm_call=insert_llm),
        crawl_repo=MagicMock(),
    )

    async def _ainvoke(state, *, config):
        # Graft a summary + an llm_call onto the state as the real spine would,
        # then run the REAL persist node against the facade-built state.
        state = dict(state)
        state["summary"] = dict(_GOOD_SUMMARY)
        state["llm_calls"] = [{"request_id": state.get("request_id"), "status": "ok"}]
        state["user_scope"] = "owner"
        state["environment"] = "prod"
        out = await persist_node_fn(state, deps=persist_deps)
        state.update(out)
        return state

    graph = MagicMock(ainvoke=_ainvoke)
    facade = _facade(graph=graph)

    out = await facade.summarize(
        PureSummaryRequest(
            content_text="pre-extracted body",
            chosen_lang="en",
            system_prompt="sys",
            correlation_id="cid-noreq",
            source_coverage="full",
        )
    )

    # The content-only path has NO request row -> request_id must be None.
    # The real persist node must therefore write NOTHING.
    finalize.assert_not_awaited()
    insert_llm.assert_not_awaited()
    index.assert_not_awaited()
    # The shaped summary dict still returns to the caller.
    assert out.get("tldr") == "t"
    assert out.get("summary_250") == "s"


async def test_content_only_summarize_completes_metadata_and_enriches_rag(monkeypatch):
    """audit #5: facade.summarize() restores LLM metadata-completion (title/author/
    dates) + RAG-field enrichment the content-only graph path lost.

    The graph returns a summary with blank metadata + no RAG fields; the facade must
    call the LLM (via the llm_client port) to fill title/author and then attach the
    pure RAG fields. The metadata-completion call carries request_id=None (no row).
    """
    from app.core.call_status import CallStatus

    llm = MagicMock(
        chat=AsyncMock(
            return_value=SimpleNamespace(
                status=CallStatus.OK,
                model="meta-model",
                response_text=None,
                response_json={
                    "choices": [
                        {"message": {"parsed": {"title": "Filled Title", "author": "A. Author"}}}
                    ]
                },
                tokens_prompt=5,
                tokens_completion=2,
                cost_usd=None,
                latency_ms=10,
                error_text=None,
            )
        )
    )
    deps = MagicMock(llm_client=llm)
    graph = MagicMock(
        ainvoke=AsyncMock(
            return_value={
                "summary": {
                    "summary_250": "A summary about distributed consensus protocols.",
                    "summary_1000": "Longer summary discussing replication and consensus.",
                    "tldr": "consensus",
                    "topic_tags": ["#systems"],
                    "metadata": {},
                }
            }
        )
    )
    facade = _facade(graph=graph, deps=deps)

    out = await facade.summarize(
        PureSummaryRequest(
            content_text="Distributed consensus coordinates replicas across nodes. " * 30,
            chosen_lang="en",
            system_prompt="sys",
            correlation_id="cid-enrich",
            source_coverage="full",
        )
    )

    # LLM metadata-completion filled the blank fields (via the port).
    llm.chat.assert_awaited_once()
    assert out["metadata"]["title"] == "Filled Title"
    assert out["metadata"]["author"] == "A. Author"
    # The metadata-completion call must NOT carry a request_id (content-only, no row).
    _, chat_kwargs = llm.chat.call_args
    assert chat_kwargs["request_id"] is None
    # RAG-field enrichment attached the retrieval fields.
    assert out.get("semantic_boosters")
    assert out.get("query_expansion_keywords")
    assert out.get("semantic_chunks")
    assert out["language"] == "en"


async def test_content_only_summarize_empty_content_raises_value_error():
    """Empty/whitespace content raises ValueError, byte-for-byte with the legacy
    ``PureSummaryService.summarize`` (the 4 callers wrap it in a StageError)."""
    facade = _facade(graph=MagicMock(ainvoke=AsyncMock()))
    with pytest.raises(ValueError, match="empty or contains only whitespace"):
        await facade.summarize(
            PureSummaryRequest(content_text="   ", chosen_lang="en", system_prompt="sys")
        )
    facade._graph.ainvoke.assert_not_awaited()


async def test_content_only_summarize_reraises_graph_failure_for_retry():
    """audit #4: a raising graph invocation must PROPAGATE (not be swallowed to {}).

    The background retry runner only retries a stage that RAISES; returning {} on
    failure silently bypassed retry_attempts. The exact exception must surface so
    the caller wraps it in a StageError and the retry loop fires.
    """
    boom = RuntimeError("graph node boom")
    facade = _facade(graph=MagicMock(ainvoke=AsyncMock(side_effect=boom)))

    with pytest.raises(RuntimeError, match="graph node boom") as exc_info:
        await facade.summarize(
            PureSummaryRequest(
                content_text="pre-extracted body",
                chosen_lang="en",
                system_prompt="sys",
                correlation_id="cid-fail",
            )
        )
    assert exc_info.value is boom


async def test_content_only_summarize_no_summary_returns_empty_dict_no_raise():
    """audit #4: the genuine no-summary case (graph completed, no summary) returns
    {} WITHOUT raising -- so the caller raises a terminal StageError with no retry."""
    facade = _facade(graph=MagicMock(ainvoke=AsyncMock(return_value={"summary": {}})))

    out = await facade.summarize(
        PureSummaryRequest(
            content_text="pre-extracted body",
            chosen_lang="en",
            system_prompt="sys",
            correlation_id="cid-empty",
        )
    )
    assert out == {}


# --------------------------------------------------------------------------- #
# (g) request row gets the owner user_id from a from_user-shaped message,
#     a telegram_messages snapshot is written, content_text + route_version set
# --------------------------------------------------------------------------- #
async def test_create_request_row_owner_id_snapshot_content_and_route_version(monkeypatch):
    from app.adapters.content.content_extractor import URL_ROUTE_VERSION

    _patch_lease(monkeypatch)
    _patch_runners(
        monkeypatch,
        streamed=AsyncMock(return_value={"summary": _GOOD_SUMMARY, "source_text": "body"}),
        plain=AsyncMock(return_value={"summary": _GOOD_SUMMARY, "source_text": "body"}),
    )

    mp = _message_persistence()
    facade = _facade(message_persistence=mp)
    await facade.handle_url_flow(_url_request())

    # The request row carries the owner user_id from ``from_user.id`` (NOT NULL) so
    # the IDOR ownership filter (rule 12) holds; chat_id from ``chat.id``; the input
    # message id from ``id``; content_text = the URL; route_version = the URL path's.
    mp.request_repo.async_create_request.assert_awaited_once()
    _, kwargs = mp.request_repo.async_create_request.call_args
    assert kwargs["user_id"] == 30
    assert kwargs["chat_id"] == 10
    assert kwargs["input_message_id"] == 20
    assert kwargs["content_text"] == "https://example.com/article"
    assert kwargs["route_version"] == URL_ROUTE_VERSION
    assert kwargs["initial_attempt_trigger"] == "initial"

    # persist-everything: a telegram_messages snapshot is written for the new row.
    mp.persist_message_snapshot.assert_awaited_once()
    snap_args, _ = mp.persist_message_snapshot.call_args
    assert snap_args[0] == 777


async def test_create_request_row_null_owner_when_no_from_user(monkeypatch):
    """A message with no ``from_user`` yields user_id=None -- the validator refuses
    to fabricate an owner from sender/sender_id (the old bug)."""
    _patch_lease(monkeypatch)
    _patch_runners(
        monkeypatch,
        streamed=AsyncMock(return_value={"summary": _GOOD_SUMMARY, "source_text": "body"}),
        plain=AsyncMock(return_value={"summary": _GOOD_SUMMARY, "source_text": "body"}),
    )

    mp = _message_persistence()
    facade = _facade(message_persistence=mp)
    # Legacy-bug shape: ``sender``/``sender_id``/``chat_id`` present but NO from_user.
    bad_msg = SimpleNamespace(chat_id=10, id=20, sender=SimpleNamespace(id=30), sender_id=30)
    await facade.handle_url_flow(_url_request(message=bad_msg))

    _, kwargs = mp.request_repo.async_create_request.call_args
    assert kwargs["user_id"] is None
    assert kwargs["chat_id"] is None  # read from ``chat.id``, not ``chat_id``


# --------------------------------------------------------------------------- #
# (g) language seed: ``_resolve_lang`` must NOT collapse ``auto`` to ``en``
# (audit #3). The graph's extract node re-resolves state['lang'] from the
# content's detected language, so a premature ``auto -> en`` collapse here would
# force every non-English article onto the English summary/prompt/cache path.
# --------------------------------------------------------------------------- #


async def test_resolve_lang_seeds_raw_auto_preference():
    cfg = _cfg()
    cfg.runtime.preferred_lang = "auto"
    facade = _facade(cfg=cfg)
    assert facade._resolve_lang(_url_request()) == "auto"


async def test_resolve_lang_passes_forced_preference_through():
    cfg = _cfg()
    cfg.runtime.preferred_lang = "ru"
    facade = _facade(cfg=cfg)
    assert facade._resolve_lang(_url_request()) == "ru"


async def test_resolve_lang_defaults_to_auto_when_preference_unset():
    cfg = _cfg()
    cfg.runtime.preferred_lang = None
    facade = _facade(cfg=cfg)
    assert facade._resolve_lang(_url_request()) == "auto"


# --------------------------------------------------------------------------- #
# (h) failure orchestration: no-summary delivery, orchestration exception
#     handler (mark-error + notify + lease=failed), terminal-failure silent
#     short-circuit, and the AcademicPaperUnavailableError paywall copy.
# --------------------------------------------------------------------------- #
async def test_graph_completes_without_summary_sends_processing_failure(monkeypatch):
    """Graph ran fine but produced no summary -> failure delivery, no follow-ups.

    Covers the ``not isinstance(summary_json, dict) or not summary_json`` branch
    that routes to ``summary_delivery.send_processing_failure`` (graph_url_processor
    :385-394) rather than the terminal-error branch (no ``error`` key present).
    """
    lease = _patch_lease(monkeypatch)
    _patch_runners(
        monkeypatch,
        streamed=AsyncMock(return_value={"summary": {}, "source_text": "body"}),
        plain=AsyncMock(return_value={"summary": {}, "source_text": "body"}),
    )

    facade = _facade()
    result = await facade.handle_url_flow(_url_request())

    assert result.success is False
    facade.summary_delivery.send_processing_failure.assert_awaited_once()
    facade.post_summary_tasks.schedule_tasks.assert_not_awaited()
    # The flow still recorded a terminal lease outcome (status=failed).
    lease.record_synchronous_outcome.assert_awaited_once()
    _, outcome_kwargs = lease.record_synchronous_outcome.call_args
    assert outcome_kwargs["status"] == "failed"


async def test_orchestration_exception_marks_error_notifies_and_leases_failed(monkeypatch):
    """A RuntimeError mid-flow -> mark request ERROR, send a generic error notice,
    and record the terminal lease as failed with the exception class as the code.

    Covers graph_url_processor :430-446 (the catch-all orchestration handler) plus
    the failed-lease outcome path (:798-801 not hit; happy outcome recorder).
    """
    lease = _patch_lease(monkeypatch)
    boom = RuntimeError("delivery exploded")
    # The runner succeeds; the failure is injected downstream in delivery so the
    # except-handler (not the terminal-error branch) fires.
    _patch_runners(
        monkeypatch,
        streamed=AsyncMock(return_value={"summary": _GOOD_SUMMARY, "source_text": "body"}),
        plain=AsyncMock(return_value={"summary": _GOOD_SUMMARY, "source_text": "body"}),
    )

    facade = _facade()
    facade._persist_bot_reply = AsyncMock(side_effect=boom)  # type: ignore[method-assign]

    result = await facade.handle_url_flow(_url_request())

    assert result.success is False
    assert result.request_id == 777
    # Request marked ERROR via the safety-net repo call.
    facade.request_repo.async_update_request_status.assert_awaited_once()
    # Generic (non-paywall) error notification surfaced with the correlation id.
    facade.response_formatter.send_error_notification.assert_awaited_once()
    notify_args, notify_kwargs = facade.response_formatter.send_error_notification.call_args
    assert notify_args[1] == "processing_failed"
    assert notify_args[2] == "cid-1"
    assert "details" not in notify_kwargs
    # The terminal lease records the failure with the exception class as the code.
    _, outcome_kwargs = lease.record_synchronous_outcome.call_args
    assert outcome_kwargs["status"] == "failed"
    assert outcome_kwargs["error_code"] == "RuntimeError"
    assert outcome_kwargs["error_message"] == "delivery exploded"


async def test_orchestration_failure_renders_paywall_copy_for_academic_error(monkeypatch):
    """An AcademicPaperUnavailableError surfaces the paper-specific paywall details
    instead of the generic template (graph_url_processor :689-700)."""
    from app.adapters.academic.platform_extractor import AcademicPaperUnavailableError

    _patch_lease(monkeypatch)
    paper_exc = AcademicPaperUnavailableError(
        reason="paywall", host="ssrn", url="https://ssrn.com/abstract=1"
    )
    _patch_runners(
        monkeypatch,
        streamed=AsyncMock(side_effect=paper_exc),
        plain=AsyncMock(side_effect=paper_exc),
    )

    facade = _facade()
    result = await facade.handle_url_flow(_url_request())

    assert result.success is False
    facade.response_formatter.send_error_notification.assert_awaited_once()
    notify_args, notify_kwargs = facade.response_formatter.send_error_notification.call_args
    assert notify_args[1] == "processing_failed"
    # Paper-specific copy (host upper-cased + reason) was passed as details.
    details = notify_kwargs["details"]
    assert "SSRN paper unavailable (paywall)" in details
    assert "paywall" in details


async def test_orchestration_failure_silent_request_suppresses_notification(monkeypatch):
    """A silent/batch flow must not emit a user-facing error notification
    (graph_url_processor :685-686 early return)."""
    _patch_lease(monkeypatch)
    boom = RuntimeError("boom")
    _patch_runners(
        monkeypatch,
        streamed=AsyncMock(side_effect=boom),
        plain=AsyncMock(side_effect=boom),
    )

    facade = _facade()
    result = await facade.handle_url_flow(_url_request(silent=True))

    assert result.success is False
    facade.response_formatter.send_error_notification.assert_not_awaited()


async def test_terminal_failure_silent_request_suppresses_processing_failure():
    """``_notify_terminal_failure`` short-circuits for an effective-silent request
    (graph_url_processor :668-669)."""
    facade = _facade()
    await facade._notify_terminal_failure(
        _url_request(silent=True),
        {"error": "Processing failed (Error ID: cid-1).", "correlation_id": "cid-1"},
    )
    facade.summary_delivery.send_processing_failure.assert_not_awaited()


async def test_mark_request_error_noop_when_no_request_id():
    """The safety-net mark-error is a no-op when the request row never materialized
    (graph_url_processor :710-711)."""
    facade = _facade()
    await facade._mark_request_error(_url_request(), None)
    facade.request_repo.async_update_request_status.assert_not_awaited()
