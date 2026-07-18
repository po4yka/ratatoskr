"""``summarize`` node -- structured summary via the llm_client port (ADR-0006/0015)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.application.dto.stream_enums import SUMMARY_TOKEN_EVENT
from app.application.graphs.summarize.deps import SummarizeConfig
from app.application.graphs.summarize.lifecycle import CallBudgetExceeded
from app.application.graphs.summarize.nodes._span import graph_node
from app.application.services.summarization.graph_llm import (
    summarize_streaming,
    summarize_with_instructor,
)
from app.application.services.summarization.graph_llm_guard import (
    GraphLLMUsageBudgetExceeded,
)
from app.core.llm_call_budget import LLMCallCapExceeded

if TYPE_CHECKING:
    from app.application.graphs.summarize.deps import SummarizeDeps
    from app.application.graphs.summarize.state import SummarizeState


@graph_node("summarize")
async def summarize(state: SummarizeState, *, deps: SummarizeDeps) -> dict[str, Any]:
    """Produce the structured summary via the llm_client port.

    Two paths share the same output shape + ``llm_calls`` record:

    - **default (ainvoke):** ``summarize_with_instructor`` -- the ported instructor
      path (sticky-failure force-fallback, en/ru instructor prompt). Byte-identical
      to legacy (T9 parity asserts this).
    - **streaming (``state['stream']``, set by the streaming runner):**
      ``summarize_streaming`` -- ONE ``chat(stream=True)`` call whose JSON deltas
      are dispatched as ``summary_token`` custom events so the T8 bridge drives
      live section previews (ADR-0017). The validate->repair loop corrects any
      malformed streamed output.

    A failed LLM call raises ``ValueError`` -> the single terminal-failure path
    (ADR-0011). No-ops when ``build_prompt`` produced no messages.

    GAP 2 (Redis LLM summary cache): when ``deps.summary_cache`` is set and
    ``state['dedupe_hash']`` is available, checks the cache before calling the LLM.
    A cache hit returns the cached summary with no ``llm_calls`` row (mirroring the
    legacy ``interactive_summary_service`` behaviour at lines 220-238). The cache
    WRITE does NOT happen here -- it happens later, in the ``persist`` node, once
    ``validate`` (and the optional ``repair`` loop) has confirmed the summary
    against the contract. Writing here, pre-validation, let a malformed-but-truthy
    LLM response poison the shared, content-hash-keyed cache for every subsequent
    request to that URL, with no eviction on repair (cache-poisoning fix). The
    streaming path bypasses cache reads -- streaming is a live-UX path and cache
    hits produce no token events.

    GAP 3a (failure llm_calls persistence): when the LLM call fails, a FAILURE
    record is attached to the exception as ``llm_failure_records`` before re-raising.
    :func:`~app.application.graphs.summarize.lifecycle.route_terminal_failure` drains
    these records into ``llm_repo`` best-effort (persist-everything rule 3).
    """
    messages = state.get("messages")
    if not messages:
        return {}

    config = deps.config if isinstance(deps.config, SummarizeConfig) else None
    provider = _provider_name(deps, config)
    model_override = (state.get("model_override") or "").strip() or None
    max_tokens = state.get("max_tokens") or None
    streamed = bool(state.get("stream"))
    lang = state.get("lang") or "en"
    dedupe_hash = state.get("dedupe_hash") or ""

    # GAP 2: cache lookup (non-streaming path only).
    if not streamed and dedupe_hash and deps.summary_cache is not None:
        cached = await deps.summary_cache.get(dedupe_hash, lang)
        if cached is not None:
            # Cache hit: no llm_calls row (mirrors legacy interactive path).
            return {
                "summary": cached,
                "call_count": state.get("call_count", 0),
                "llm_calls": [],
            }

    if streamed:
        try:
            summary, call_metas, call_count = await summarize_streaming(
                llm_client=deps.llm_client,
                messages=messages,
                source_content=state.get("content_for_summary") or "",
                max_tokens=max_tokens,
                model_override=model_override,
                temperature=config.temperature if config else 0.2,
                structured_output_mode=config.structured_output_mode if config else None,
                on_token=_dispatch_summary_token,
                request_id=state.get("request_id"),
                correlation_id=state.get("correlation_id"),
                guard=getattr(deps, "llm_guard", None),
                current_call_count=state.get("call_count", 0),
            )
        except (LLMCallCapExceeded, GraphLLMUsageBudgetExceeded) as exc:
            raise CallBudgetExceeded(str(exc)) from exc
        except Exception as exc:
            # GAP 3a: attach failure record then re-raise.
            raise _tag_failure(state, config, exc, structured=False, provider=provider) from exc
    else:
        try:
            summary, call_metas, call_count = await summarize_with_instructor(
                llm_client=deps.llm_client,
                messages=messages,
                source_content=state.get("content_for_summary") or "",
                max_tokens=max_tokens,
                model_override=model_override,
                temperature=config.temperature if config else 0.2,
                max_retries=config.summarization_max_retries if config else 3,
                sticky_fallback_enabled=config.sticky_fallback_enabled if config else True,
                structured_output_mode=config.structured_output_mode if config else None,
                correlation_id=state.get("correlation_id"),
                request_id=state.get("request_id"),
                guard=getattr(deps, "llm_guard", None),
                current_call_count=state.get("call_count", 0),
            )
        except (LLMCallCapExceeded, GraphLLMUsageBudgetExceeded) as exc:
            raise CallBudgetExceeded(str(exc)) from exc
        except Exception as exc:
            # GAP 3a: attach failure record then re-raise.
            raise _tag_failure(state, config, exc, structured=True, provider=provider) from exc

    result: dict[str, Any] = {
        "summary": summary,
        "call_count": call_count,
        "llm_calls": [
            _call_record(
                state,
                config,
                call_meta,
                status=str(call_meta.get("status") or "ok"),
                structured=not streamed,
                error_text=(str(call_meta["error_text"]) if call_meta.get("error_text") else None),
                provider=provider,
            )
            for call_meta in call_metas
        ],
    }

    return result


async def _dispatch_summary_token(delta: str) -> None:
    """Emit one summary token delta as a langgraph custom event (ADR-0017).

    Best-effort, per-token side-channel: streamed tokens are ephemeral and must
    NEVER fail the authoritative summary (ADR-0011/0017). When invoked outside an
    ``astream_events`` callback context (or without the graph extra installed),
    the dispatch is silently skipped. langchain_core is imported lazily so this
    node stays importable without the ``graph`` extra (no-graph-extra invariant).
    """
    if not delta:
        return
    try:
        from langchain_core.callbacks import adispatch_custom_event

        await adispatch_custom_event(SUMMARY_TOKEN_EVENT, delta)
    except Exception:
        return


def _call_record(
    state: SummarizeState,
    config: SummarizeConfig | None,
    call_meta: dict[str, Any],
    *,
    status: str,
    structured: bool = True,
    error_text: str | None = None,
    provider: str = "unknown",
) -> dict[str, Any]:
    """Build the serializable ``llm_calls`` record (attempt_trigger='graph_node')."""
    rec: dict[str, Any] = {
        "request_id": state.get("request_id"),
        "provider": provider,
        "model": call_meta.get("model"),
        "tokens_prompt": call_meta.get("tokens_prompt"),
        "tokens_completion": call_meta.get("tokens_completion"),
        "cost_usd": call_meta.get("cost_usd"),
        "latency_ms": call_meta.get("latency_ms"),
        "fallback_model_used": _fallback_model(config, call_meta),
        "status": status,
        "structured_output_used": structured,
        "structured_output_mode": config.structured_output_mode if config else None,
        "attempt_trigger": "graph_node",
    }
    if error_text is not None:
        rec["error_text"] = error_text
    return rec


def _fallback_model(config: SummarizeConfig | None, call_meta: dict[str, Any]) -> str | None:
    """Persist a selected fallback when the provider returned a non-primary model."""
    model = call_meta.get("model")
    if not isinstance(model, str) or not model:
        return None
    return model if config is not None and model != config.model else None


def _tag_failure(
    state: SummarizeState,
    config: SummarizeConfig | None,
    exc: Exception,
    *,
    structured: bool,
    provider: str,
) -> Exception:
    """Attach an llm_calls failure record to ``exc`` and return it for re-raising.

    :func:`~app.application.graphs.summarize.lifecycle.route_terminal_failure`
    reads ``exc.llm_failure_records`` and drains the rows into ``llm_repo``
    best-effort (GAP 3a / persist-everything rule 3). Mutating ``exc.__dict__``
    is safe: the exception is local to this call and only the terminal handler
    reads the attribute.

    FIX-3: reads ``exc.__llm_result__`` (set by ``graph_llm.py`` before raising)
    to populate the failure record with real model / error_text / latency_ms /
    token counts. Falls back to the config model (never None per rule 11) and
    ``str(exc)`` when no raw result is available (e.g. network timeout before
    any response). Per-model cascade rows (legacy ``auto_backfill``) are not
    reproduced here -- the instructor adapter collapses them into one result;
    a comment is left for future fidelity work.
    """
    physical_attempts = getattr(exc, "__llm_physical_attempts__", None)
    if isinstance(physical_attempts, list) and physical_attempts:
        records = [
            _call_record(
                state,
                config,
                attempt,
                status=str(attempt.get("status") or "error"),
                structured=structured,
                error_text=str(attempt.get("error_text") or exc),
                provider=provider,
            )
            for attempt in physical_attempts
            if isinstance(attempt, dict)
        ]
        exc.__dict__.setdefault("llm_failure_records", []).extend(records)
        return exc

    # Surface the raw LLMCallResult from the exception when available.
    llm_result = getattr(exc, "__llm_result__", None)
    if llm_result is not None:
        raw_model = getattr(llm_result, "model", None) or getattr(llm_result, "model_used", None)
        raw_error = getattr(llm_result, "error_text", None) or str(exc)
        call_meta: dict[str, Any] = {
            "model": raw_model or (config.model if config else None),
            "tokens_prompt": getattr(llm_result, "tokens_prompt", None),
            "tokens_completion": getattr(llm_result, "tokens_completion", None),
            "cost_usd": getattr(llm_result, "cost_usd", None),
            "latency_ms": getattr(llm_result, "latency_ms", None),
        }
    else:
        # No raw result (e.g. timeout before first byte, or structured-mode
        # adapter did not attach __llm_result__). Fall back to config model
        # (never None per rule 11) so the row is always queryable by model.
        # NOTE: per-model cascade rows (legacy auto_backfill) are collapsed here;
        # a future fidelity pass should replicate them from the instructor result.
        call_meta = {
            "model": config.model if config else None,
            "tokens_prompt": None,
            "tokens_completion": None,
            "cost_usd": None,
            "latency_ms": None,
        }
        raw_error = str(exc)

    failure_record = _call_record(
        state,
        config,
        call_meta,
        status="error",
        structured=structured,
        error_text=raw_error,
        provider=provider,
    )
    exc.__dict__.setdefault("llm_failure_records", []).append(failure_record)
    return exc


def _provider_name(deps: SummarizeDeps, config: SummarizeConfig | None) -> str:
    provider = getattr(deps.llm_client, "provider_name", None)
    if isinstance(provider, str) and provider:
        return provider
    return config.llm_provider if config else "unknown"
