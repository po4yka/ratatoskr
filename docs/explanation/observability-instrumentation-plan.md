# Ratatoskr Backend: Phased Observability and Telemetry Instrumentation Plan

**Date:** 2026-05-30
**Scope:** `ratatoskr/` backend only. Covers OTel tracing gaps, Prometheus metric call-site gaps, attribute namespace, blocking-call instrumentation, and operator decisions.

---

## 1. Gap Analysis: Existing OTel Instrumentation vs Missing

### SDK Wiring Status

The OTel SDK is wired and operational today. `init_tracing()` is called in five entry-points — `bot.py:25`, `app/api/main.py:96`, `app/tasks/broker.py:22`, `app/tasks/scheduler.py:15`, and `app/mcp/server.py:112`. The function is idempotent (`_initialized` guard at `app/observability/otel.py:74`). When `OTEL_ENABLED` is falsy or the `opentelemetry-sdk` package is absent, every call degrades to a `_NoOpTracer`/`_NoOpSpan` with no user-visible effect. Auto-instrumentation is active for HTTPX, Redis, and logging. The OTLP exporter targets `OTEL_EXPORTER_OTLP_ENDPOINT` (defaulting to `http://tempo:4317`).

### What Is Instrumented Today

| Layer | Span | Prometheus metrics |
|---|---|---|
| Telegram intake | `telegram.update` with `ratatoskr.correlation_id` (`message_router.py:154`) | None at message-router level |
| URL orchestration | `url_flow.process` with url and correlation_id (`graph_url_processor.py::_run_url_flow`) | `record_llm_request_total_latency` in finally-block (`_run_url_flow_inner`) |
| Scraper chain (chain level) | `scraper.chain` with url and mode (`:225`); sets `scraper.winner` and `scraper.attempts` on success and exhaustion paths | `record_scraper_chain_total_latency` in `_record_outcome` closure (`:186`) |
| Scraper chain (per-rung level) | `scraper.<name>` span opened at `chain.py:468`; outcome set at lines 479/482/506/520/543/561/565 | None — `record_scraper_attempt` and `record_scraper_attempt_latency` defined in `metrics.py:1075` and `:1089` but have zero call sites anywhere in the scraper package |
| LLM client `chat()` | `llm.chat` span at `openrouter_client.py:511`; sets `cost_usd` and `latency_ms` only | `record_per_model_latency`, `record_per_model_timeout`, `record_per_model_circuit_breaker_state` called across `chat_engine.py` |
| LLM client `chat_structured()` | `llm.chat_structured` span at `openrouter_client.py:556`; sets only `llm.provider` and `llm.model`; result is a bare `return await …` with no assigned variable, so no post-call attributes can be set without restructuring | Same Prometheus coverage as `chat()` via `chat_engine.py` |
| LLM streaming | No span | `record_stream_latency_ms`, `record_draft_stream_event` in `chat_streaming.py:102/105/129`; stream fallback counters in `chat_attempt_runner.py:185` and `chat_transport.py:364` |
| LLM storage | No span | `record_llm_call_persisted` at `llm_response_workflow_storage.py:83`, `:174`, `:183` |
| Aggregation | No span | `record_aggregation_bundle` at `multi_source_extraction_agent.py:294`; `record_aggregation_extraction` at `:454` |
| Agents (all) | No span anywhere in `app/agents/` | `repo_analysis_agent.py` has no metric calls at LLM call sites |
| Embedding / Qdrant | No span | `record_vector_write` on failure paths in `qdrant_store.py:357` (upsert), `:417` (replace), `:509` (delete); `record_vector_index_lag` at `reconciliation.py:222`; no success-path counters, no latency histograms |

### Confirmed Gaps

1. **Scraper per-rung Prometheus metrics**: `record_scraper_attempt` and `record_scraper_attempt_latency` have no call sites. The two helpers are dead exports.
2. **LLM token and cost span attributes**: `llm.chat` span (`:511`) is missing `llm.prompt_tokens`, `llm.completion_tokens`, `llm.tokens_total`, and `ratatoskr.correlation_id`. `llm.chat_structured` span (`:556`) sets nothing post-call and requires code restructuring before it can.
3. **Root span enrichment**: `telegram.update` span lacks `interaction_type`, `has_forward`, `chat_id`, and `source_type` attributes — these are available after `prepare()` returns at `message_router.py:161`.
4. **Agent layer**: no span in any agent `execute()`. `repo_analysis_agent.py` has zero Prometheus calls around `chat_structured` at `:169` and legacy `call()` at `:256`. `web_search_agent.py:205` has zero calls to `record_llm_call_attempt` or `record_llm_call_latency`.
5. **LLM retry exhaustion counter**: `record_llm_call_retry_exhaustion` defined at `metrics.py:993` is never called. The exhaustion point is `chat_engine.py:419`.
6. **Embedding latency**: no histogram around the blocking `asyncio.to_thread(model.encode, …)` calls at `embedding_service.py:115` (single) and `:129` (batch), nor around the Gemini retry loop at `gemini_embedding_service.py:135`.
7. **Qdrant operation latency and success counters**: `record_vector_write` fires only on failure; success path and round-trip latency histogram are absent for upsert (`qdrant_store.py:348`), replace (`:397`), and query (`:455`).
8. **Concurrency / queue gauges**: no gauge tracks how many requests are currently in-flight through `URLProcessor` or waiting in the Taskiq queue.

### Mapper Claims That Did Not Verify

The following specific line-number claims from the pre-verification mapper were rejected:

- `url_processor.py:542` — actual metric call is at `:543` (`:542` is the import statement).
- `url_handler.py:523` — covers only the error path; success call is at `:593`.
- `pure_summary_service.py:199` — semaphore context manager; actual `chat_structured` await is at `:200`.
- `content_chunker.py:155` and `:246` — same semaphore issue; actual awaits are at `:160` and `:249`.
- `chain.py:458` — the `_record` helper definition, not a call site; insertion target is the downstream call sites at `:479`, `:494`, `:562`, `:565`.
- `qdrant_store.py:416` — logger call; `record_vector_write` is at `:417`.
- `qdrant_store.py:193`, `:203`; `embedding_service.py:93`; `gemini_embedding_service.py:69`; `vector_search_service.py:125`; `embedding_generation.py:167`; `summary_embedding_generator.py:130`; `repository_embedding.py:142`; `git_mirror_readme_indexer.py:172` — all off by one; every structured-log cite was the `logger.info(` call-open line, not the event-name string line.
- `qdrant_store.py:396` — the `client = self._client` assignment; `try:` block starts at `:397`.
- `openrouter_client.py:563` — valid injection site for `llm.chat_structured` attributes requires restructuring the bare `return await self._chat_structured_impl(…)` into a two-step assign-then-return before span attributes can be set.
- Provider-level spans proposed in six scraper provider files (`scrapling_provider.py:109`, `defuddle_provider.py:65`, `firecrawl_provider.py:41`, `direct_html_provider.py:48`, `direct_pdf_provider.py:76`, `playwright_provider.py:52`) would create nested duplicates because `chain.py:468-474` already opens a `scraper.<name>` span wrapping each provider call. The correct fix is to enrich the existing chain-side span, not add inner spans.

---

## 2. Telemetry Backends Actually Deployed

`docs/guides/optimize-performance.md` does not reference the monitoring stack. `docs/explanation/observability-strategy.md` is referenced from SPEC but its content was not enumerated — the compose files are the source of truth.

| Backend | Status | Compose profile (docker-compose.yml) | Compose profile (docker-compose.monitoring.yml) | Config file |
|---|---|---|---|---|
| Prometheus | Deployed | `with-monitoring` (`:912-937`) | Always-on (`:20-50`) | `ops/monitoring/prometheus.yml` |
| Grafana | Deployed | `with-monitoring` (`:939-963`) | Always-on (`:52-80`) | `ops/monitoring/grafana/provisioning/` |
| Loki | Deployed | `with-monitoring` (`:964-980`) | Always-on (`:82-104`) | `ops/monitoring/loki-config.yml` |
| Promtail | Deployed | `with-monitoring` (`:982-995`) | Always-on (`:106-124`) | `ops/monitoring/promtail-config.yml` |
| Tempo | Partially deployed | Absent from `docker-compose.yml` — referenced only via `OTEL_EXPORTER_OTLP_ENDPOINT=http://tempo:4317` on application services (`:79`, `:182`, `:271`, `:343`, `:419`) | Always-on (`:148-173`) | `ops/monitoring/tempo-config.yml` |
| node-exporter | Deployed | `with-monitoring` (`:997-1011`) | Always-on (`:126-146`) | Scraped at `node-exporter:9100` per `prometheus.yml:38` |
| OTel Collector | Absent | Not present in any compose file | Not present | None — apps export OTLP directly to Tempo |

**Operational note on Tempo:** when running `docker-compose.yml` with `--profile with-monitoring`, the application services emit OTLP to `http://tempo:4317` but no Tempo container is started. Traces are silently dropped. To receive traces on this stack, either add a Tempo service block to `docker-compose.yml` under the `with-monitoring` profile or use `docker-compose.monitoring.yml` as the compose file.

---

## 3. Phased Implementation Plan

Phases are ordered by analytics value for a single-tenant operator who processes tens of requests per day. Each phase is independently deployable. All helpers referenced below pre-exist in `app/observability/metrics.py` and `app/observability/otel.py`; no new metric families are introduced until Phase 4.

---

### Phase 1: Scraper Per-Rung Prometheus Metrics

**Analytics question answered:** Which scraper provider is slow, failing, or timing out? What fraction of chain invocations succeed at each rung?

Two pre-existing helpers have zero call sites: `record_scraper_attempt` (`metrics.py:1075`) and `record_scraper_attempt_latency` (`metrics.py:1089`). All changes are inside `app/adapters/content/scraper/chain.py`.

The chain's `_attempt_provider()` inner function already records outcome via a `_record(status, error_class)` helper. `record_scraper_attempt` and `record_scraper_attempt_latency` calls must be co-located with existing `_record()` call sites. Providers that catch `TimeoutError` and return a `FirecrawlResult(ERROR)` without re-raising (scrapling, defuddle, playwright) do not propagate through the chain's exception handler, so provider-level metric calls inside those handlers would not duplicate chain-level calls.

| File:line | What to add | Attributes / parameters | Analytics question |
|---|---|---|---|
| `chain.py:19` (imports) | `from app.observability.metrics import record_scraper_attempt, record_scraper_attempt_latency` | — | Prerequisite import |
| `chain.py:479` (CancelledError path in `_attempt_provider`) | `record_scraper_attempt(provider=name, status="cancelled")` | `provider`, `status` | How often are provider attempts cancelled? |
| `chain.py:494` (exception path in `_attempt_provider`) | `record_scraper_attempt(provider=name, status="error"); record_scraper_attempt_latency(provider=name, latency_seconds=elapsed)` | `provider`, `status`, `latency_seconds` | Which provider raises exceptions and at what latency? |
| `chain.py:562` (success path in `_attempt_provider`) | `record_scraper_attempt(provider=name, status="success"); record_scraper_attempt_latency(provider=name, latency_seconds=elapsed)` | `provider`, `status`, `latency_seconds` | Success rate and latency distribution per provider |
| `chain.py:565` (no-content path in `_attempt_provider`) | `record_scraper_attempt(provider=name, status="no_content")` | `provider`, `status` | Which providers return empty results vs error? |
| `chain.py:468` (existing `provider_span` open) | Add `span.set_attribute("scraper.tier", tier)`, `span.set_attribute("scraper.timeout_sec", timeout)`, `span.set_attribute("scraper.request_id", str(request_id))` after span context is entered | `scraper.tier`, `scraper.timeout_sec`, `scraper.request_id` | Trace which tier a rung belongs to; correlate spans to requests |

The last row enriches the existing span rather than opening a new one, avoiding nested-span duplication.

---

### Phase 2: LLM Token/Cost Metrics and Span Attributes

**Analytics question answered:** What are the per-request token costs? Which model is actually serving responses after fallback? Is the retry-exhaustion counter firing?

#### 2a. Enrich `llm.chat` span with token and correlation attributes

`openrouter_client.py:534` is the valid injection site — the result is an `LLMCallResult` with confirmed fields `tokens_prompt`, `tokens_completion`, `model`, `cache_read_tokens`, `cache_creation_tokens`.

| File:line | What to add | Attributes | Analytics question |
|---|---|---|---|
| `openrouter_client.py:534` (inside `if hasattr(result, "cost_usd")` block) | `span.set_attribute("ratatoskr.llm.tokens_prompt", result.tokens_prompt or 0)`, `span.set_attribute("ratatoskr.llm.tokens_completion", result.tokens_completion or 0)`, `span.set_attribute("ratatoskr.llm.model_served", result.model or self._model)` | `ratatoskr.llm.tokens_prompt`, `ratatoskr.llm.tokens_completion`, `ratatoskr.llm.model_served` | Token consumption per span; which model actually served after fallback |
| `openrouter_client.py:511` (immediately after span open, use existing `set_correlation_id_attr`) | `from app.observability.otel import set_correlation_id_attr; set_correlation_id_attr(self._correlation_id)` if correlation_id is threaded to this site | `ratatoskr.correlation_id` | Link LLM traces to originating Telegram request |

#### 2b. Restructure `llm.chat_structured` to permit post-call attributes

The current code at `openrouter_client.py:556-563` is a bare `return await …` inside a `with` block. No attributes can be set without assigning the result.

| File:line | What to add | Notes |
|---|---|---|
| `openrouter_client.py:563` | Restructure to: `result = await self._chat_structured_impl(…)` on one line, then set span attributes, then `return result` | This is a required code-shape change before any span attributes can be added for the structured path |

After restructuring, add `span.set_attribute("ratatoskr.llm.model_served", result.model or self._model)` and `span.set_attribute("ratatoskr.llm.cost_usd", result.cost_usd or 0.0)`.

#### 2c. Wire the dead retry-exhaustion counter

`record_llm_call_retry_exhaustion` (`metrics.py:993`) is defined but has zero call sites in the entire codebase.

| File:line | What to add | Attributes | Analytics question |
|---|---|---|---|
| `chat_engine.py:419` (exhaustion point after loop exit) | `from app.observability.metrics import record_llm_call_retry_exhaustion; record_llm_call_retry_exhaustion(model=context.models_to_try[-1] if context.models_to_try else "unknown")` | `model` | How often does the full model cascade exhaust without a result? |

#### 2d. Add fallback-rung span attributes on terminal result

| File:line | What to add | Attributes | Analytics question |
|---|---|---|---|
| `chat_engine.py:346` (terminal result return) | `span.set_attribute("ratatoskr.llm.fallback_rung_index", model_index)`, `span.set_attribute("ratatoskr.llm.models_attempted_count", len(model_state.models_attempted or []))` | `ratatoskr.llm.fallback_rung_index`, `ratatoskr.llm.models_attempted_count` | Did fallback activate? How deep into the cascade did this request go? |

---

### Phase 3: Root Span Enrichment and Correlation ID Propagation

**Analytics question answered:** What is the breakdown of processed interaction types (URL, forward, command)? Where in the request lifecycle does latency accumulate?

#### 3a. Enrich the root Telegram span

`telegram.update` opens at `message_router.py:154`. After `prepare()` returns `route_context` at approximately line 161, the following attributes are available.

| File:line | What to add | Attributes | Analytics question |
|---|---|---|---|
| `message_router.py:161` (after prepare(), inside existing span context) | `span.set_attribute("ratatoskr.telegram.interaction_type", route_context.interaction_type)`, `span.set_attribute("ratatoskr.telegram.has_forward", route_context.has_forward)`, `span.set_attribute("ratatoskr.telegram.chat_id", str(chat_id))`, `span.set_attribute("ratatoskr.telegram.source_type", route_context.source_type or "unknown")` | `ratatoskr.telegram.interaction_type`, `ratatoskr.telegram.has_forward`, `ratatoskr.telegram.chat_id`, `ratatoskr.telegram.source_type` | What fraction of updates are URL vs forward vs command? Are forwards slower? |

#### 3b. Propagate correlation_id to the `url_flow.process` span

The span in `graph_url_processor.py::_run_url_flow` already sets `ratatoskr.correlation_id` as a direct attribute. Verify `set_correlation_id_attr` from `otel.py:137` is also called inside this span so Tempo's baggage-propagation picks it up consistently (the span attribute alone is not propagated to child spans unless also set via `set_correlation_id_attr`).

| File:line | What to add | Notes |
|---|---|---|
| `graph_url_processor.py::_run_url_flow` (inside existing span context, after span opens) | `from app.observability.otel import set_correlation_id_attr; set_correlation_id_attr(request.correlation_id)` | Sets the active-span attribute via the standard helper, consistent with `message_router.py:160` pattern |

#### 3c. Add `url_flow.cache_hit` span event for cache-served responses

`cached_summary_responder.py:54` handles the cache hit path but emits no trace event.

| File:line | What to add | Attributes | Analytics question |
|---|---|---|---|
| `cached_summary_responder.py:54` (after `dedupe_hash = compute_dedupe_hash(url_text)`) | On the cache-hit branch (approximately line 57–65), call `tracer.start_as_current_span("url_flow.cache_hit", attributes={"ratatoskr.request.dedupe_hash": dedupe_hash})` as a short span | `ratatoskr.request.dedupe_hash` | What fraction of requests are served from cache? |

---

### Phase 4: Agent Layer Instrumentation

**Analytics question answered:** Where does latency accumulate inside the agent graph? How often does the self-correction loop fire? Which agent is the bottleneck?

All agents in `app/agents/` have zero OTel spans and zero Prometheus LLM metric calls.

#### 4a. Base agent root span

| File:line | What to add | Attributes | Analytics question |
|---|---|---|---|
| `base_agent.py:56` (`execute()` abstract method) | In each concrete implementation, wrap `execute()` body with `_tracer.start_as_current_span(f"agent.{agent_name}", attributes={"ratatoskr.correlation_id": …})` using `get_tracer(__name__)` | `ratatoskr.correlation_id`, `ratatoskr.agent.name` | Which agent is called and how long does it take? |

#### 4b. ContentExtractionAgent

| File:line | What to add | Attributes | Analytics question |
|---|---|---|---|
| `content_extraction_agent.py:60` (execute body) | Span `agent.content_extraction` with `ratatoskr.correlation_id` | `ratatoskr.correlation_id` | Extraction agent total latency |
| `content_extraction_agent.py:110` (`_extract_with_validation`) | Child span `agent.content_extraction.validate` | — | How much time is spent in post-extraction validation? |

#### 4c. ValidationAgent

| File:line | What to add | Attributes | Analytics question |
|---|---|---|---|
| `validation_agent.py:45` (execute body) | Span `agent.validation`; add a `validation_failures_total` counter (new metric family, low-cardinality label: `reason`) | `ratatoskr.correlation_id`, `reason` | How often does validation fail and why? |

#### 4d. WebSearchAgent LLM calls

| File:line | What to add | Attributes | Analytics question |
|---|---|---|---|
| `web_search_agent.py:205` (around `chat_structured` call) | `record_llm_call_attempt(provider="openrouter", model=model, status="success"/"error")` and `record_llm_call_latency(model=model, latency_seconds=elapsed)` | `provider`, `model`, `status` | How much LLM latency does web-search enrichment add? |

#### 4e. RepoAnalysisAgent LLM calls

For `repo_analysis_agent.py`, the cleanest instrumentation point is inside `_persist()` at line 367 where `async_insert_llm_call` is called. Wiring `record_llm_call_persisted(call_dict)` there drives eight metric families through one call site and avoids double-counting against any future direct call-site additions at `:169` and `:256`.

| File:line | What to add | Attributes | Analytics question |
|---|---|---|---|
| `repo_analysis_agent.py:367` (inside `_persist()`, after `async_insert_llm_call`) | `from app.observability.metrics import record_llm_call_persisted; record_llm_call_persisted(call_payload)` where `call_payload` is the same dict passed to `async_insert_llm_call` | Driven by `record_llm_call_persisted` contract (8 metric families) | Token/cost/latency for repo analysis in the same Prometheus panels as URL pipeline LLM calls |

#### 4f. CombinedSummaryAgent

| File:line | What to add | Attributes | Analytics question |
|---|---|---|---|
| `combined_summary_agent.py:66` (execute body) | Span `agent.combined_summary` with `ratatoskr.correlation_id` | `ratatoskr.correlation_id` | Combined summary path latency |

---

### Phase 5: Embedding, Qdrant, and Concurrency Gauges

**Analytics question answered:** Is Qdrant the bottleneck? Are embeddings taking too long? Is the `asyncio.to_thread` pool saturated?

#### 5a. Qdrant operation latency histograms and success counters

The `record_vector_write` helper currently fires only on failure. The `VECTOR_WRITES_TOTAL` counter at `metrics.py:492` accepts an `operation` and `status` label — adding `status="success"` calls activates the existing counter definition.

| File:line | What to add | Attributes | Analytics question |
|---|---|---|---|
| `qdrant_store.py:350` (after successful `client.upsert()` inside try block) | `record_vector_write(operation="upsert", status="success")` | `operation`, `status` | What is the upsert success rate? |
| `qdrant_store.py:348` (wrap `client.upsert()` with `time.perf_counter()`) | `record_db_query(operation="qdrant_upsert", latency_seconds=elapsed)` using the existing `record_db_query` helper from `metrics.py:696` | `operation` | Qdrant upsert round-trip latency distribution |
| `qdrant_store.py:397` (inside replace try block, after successful operation) | `record_vector_write(operation="replace", status="success")` | `operation`, `status` | Replace success rate |
| `qdrant_store.py:397` (wrap replace operation with timer) | `record_db_query(operation="qdrant_replace", latency_seconds=elapsed)` | `operation` | Replace latency |
| `qdrant_store.py:455` (wrap `client.query_points()` with timer) | `record_db_query(operation="qdrant_query", latency_seconds=elapsed)` | `operation` | Qdrant query latency — is semantic search fast enough? |

`record_db_query` at `metrics.py:696` observes `DB_QUERY_LATENCY` which is a Prometheus histogram already scraped. Using this helper for Qdrant operations is semantically reasonable and avoids defining a new metric family.

#### 5b. Embedding call latency

| File:line | What to add | Attributes | Analytics question |
|---|---|---|---|
| `embedding_service.py:115` (wrap `asyncio.to_thread(model.encode, …)`) | Timer around the `to_thread` call; `record_db_query(operation="embedding_encode_single", latency_seconds=elapsed)` | `operation` | How long does a single embedding generation block the thread pool? |
| `embedding_service.py:129` (wrap `asyncio.to_thread(model.encode, list(texts), …)`) | Timer; `record_db_query(operation="embedding_encode_batch", latency_seconds=elapsed)` | `operation` | Batch embedding throughput |
| `gemini_embedding_service.py:135` (inside `_embed_contents_with_retry` retry loop) | Timer per attempt; `record_db_query(operation="gemini_embedding", latency_seconds=elapsed)` | `operation` | Gemini embedding latency per attempt vs retry index |

#### 5c. Concurrency and queue gauges (new metrics — lowest priority)

These require new metric definitions because no existing helper covers in-flight request counts or queue depth.

Add to `app/observability/metrics.py`:

```python
URL_PROCESSOR_IN_FLIGHT = Gauge(
    "ratatoskr_url_processor_in_flight",
    "Number of URL processing requests currently active",
    registry=REGISTRY,
)
```

Wire as a context manager increment/decrement at `graph_url_processor.py::_run_url_flow_inner` (span open, increment) and its finally-block (decrement); `set_url_processor_in_flight` is already defined in `app/observability/metrics.py` and called there. This is a single-process gauge — valid for the current architecture where one Docker container handles all requests.

---

## 4. Unified Attribute Namespace Proposal

Propose a new constants module at `app/observability/attributes.py`. This module has no runtime dependencies and can be imported freely.

```python
# app/observability/attributes.py
"""
Canonical ratatoskr.* OTel span attribute keys.

All span.set_attribute() calls across the codebase should reference
these constants rather than inline strings.
"""

# -- Request / Correlation --
REQUEST_CORRELATION_ID = "ratatoskr.correlation_id"
REQUEST_DEDUPE_HASH    = "ratatoskr.request.dedupe_hash"
REQUEST_SOURCE_TYPE    = "ratatoskr.request.source_type"

# -- Telegram --
TELEGRAM_INTERACTION_TYPE = "ratatoskr.telegram.interaction_type"
TELEGRAM_HAS_FORWARD      = "ratatoskr.telegram.has_forward"
TELEGRAM_CHAT_ID          = "ratatoskr.telegram.chat_id"
TELEGRAM_SOURCE_TYPE      = "ratatoskr.telegram.source_type"

# -- Scraper --
SCRAPER_PROVIDER     = "ratatoskr.scraper.provider"
SCRAPER_MODE         = "ratatoskr.scraper.mode"
SCRAPER_TIER         = "ratatoskr.scraper.tier"
SCRAPER_TIMEOUT_SEC  = "ratatoskr.scraper.timeout_sec"
SCRAPER_WINNER       = "ratatoskr.scraper.winner"
SCRAPER_ATTEMPTS     = "ratatoskr.scraper.attempts"
SCRAPER_CONTENT_LEN  = "ratatoskr.scraper.content_len"
SCRAPER_REQUEST_ID   = "ratatoskr.scraper.request_id"

# -- LLM --
LLM_PROVIDER            = "ratatoskr.llm.provider"
LLM_MODEL               = "ratatoskr.llm.model"
LLM_MODEL_SERVED        = "ratatoskr.llm.model_served"
LLM_TOKENS_PROMPT       = "ratatoskr.llm.tokens_prompt"
LLM_TOKENS_COMPLETION   = "ratatoskr.llm.tokens_completion"
LLM_TOKENS_TOTAL        = "ratatoskr.llm.tokens_total"
LLM_COST_USD            = "ratatoskr.llm.cost_usd"
LLM_LATENCY_MS          = "ratatoskr.llm.latency_ms"
LLM_FALLBACK_RUNG_INDEX      = "ratatoskr.llm.fallback_rung_index"
LLM_MODELS_ATTEMPTED_COUNT   = "ratatoskr.llm.models_attempted_count"
LLM_CACHE_READ_TOKENS        = "ratatoskr.llm.cache_read_tokens"
LLM_CACHE_CREATION_TOKENS    = "ratatoskr.llm.cache_creation_tokens"

# -- Agent --
AGENT_NAME         = "ratatoskr.agent.name"
AGENT_ATTEMPT      = "ratatoskr.agent.attempt"

# -- Source --
SOURCE_TYPE        = "ratatoskr.source.type"   # url | youtube | twitter | academic | forward
SOURCE_URL         = "ratatoskr.source.url"

# -- Vector / Embedding --
VECTOR_OPERATION   = "ratatoskr.vector.operation"
VECTOR_STATUS      = "ratatoskr.vector.status"
EMBEDDING_MODEL    = "ratatoskr.embedding.model"
EMBEDDING_DIMS     = "ratatoskr.embedding.dims"
```

**Migration note:** `chain.py:225` currently uses the inline strings `"scraper.url"` and `"scraper.mode"`. These should be renamed to `ratatoskr.scraper.url` (via `SOURCE_URL`) and `ratatoskr.scraper.mode` (via `SCRAPER_MODE`) when the attributes module is introduced. The existing `llm.provider` and `llm.model` in `openrouter_client.py:511` should similarly migrate to `LLM_PROVIDER` and `LLM_MODEL`. Treat the renaming as a separate PR from the gap-filling to avoid span-query churn in Grafana during the transition.

---

## 5. Blocking-Call Hotlist

These calls run on the asyncio event loop thread or block the thread pool in ways that distort wall-clock latency measurements if not wrapped in instrumentation.

| File:line | Blocking operation | Risk | Instrumentation action |
|---|---|---|---|
| `embedding_service.py:84` | `SentenceTransformer(model_name)` — synchronous model load, 0.5–5 s on cold start; runs on the event loop if called outside `to_thread` | Blocks event loop entirely if `_ensure_model()` is not already called via `to_thread`; cold-start tail latency not captured | Wrap in a short span `embedding.model_load` using `get_tracer(__name__)`, triggered at service startup via DI warmup rather than lazy-on-first-request |
| `embedding_service.py:115` | `asyncio.to_thread(model.encode, text, …)` — sentence-transformers inference, 20–200 ms per text on CPU | Thread-pool saturation under concurrent embedding requests; latency not measured | Timer + `record_db_query(operation="embedding_encode_single", …)` as described in Phase 5 |
| `embedding_service.py:129` | `asyncio.to_thread(model.encode, list(texts), …)` — batch encode; scales linearly with batch size | Same thread-pool concern; backfill CLI runs batches of 50 by default | Timer + `record_db_query(operation="embedding_encode_batch", …)` |
| `gemini_embedding_service.py:135` | `asyncio.to_thread(client.models.embed_content, …)` — synchronous Gemini SDK call inside retry loop | Each retry adds latency; rate-limit retries can stack; log warning exists but no Prometheus counter | Timer + `record_db_query(operation="gemini_embedding", …)` per attempt |
| `app/adapters/youtube/download_pipeline.py:100` | `asyncio.to_thread(self._download_video_sync, …)` inside `asyncio.timeout(600.0)` at `:99` | 600 s timeout is the entire wall-clock budget; no intermediate progress or partial-latency span | Wrap in span `youtube.download` opening at `:99`; set `ratatoskr.source.url` attribute |
| `app/adapters/youtube/youtube_downloader_parts/transcript_api.py:59` (per verification, line 59 is a for-loop over retry attempts; exact await line was rejected and requires re-verification) | `YouTubeTranscriptApi.get_transcript(…)` — synchronous HTTP via the transcript API; no async wrapper confirmed | Blocks event loop if called outside `to_thread`; verify whether a `to_thread` wrapper exists at the call site before adding instrumentation | Verify call chain; if synchronous and on-loop, add `asyncio.to_thread` wrapper and a span |
| `app/infrastructure/embedding/embedding_service.py:84` | `SentenceTransformer(model_name)` model load (repeated for completeness) | See row 1 | See row 1 |

The six entries above are the hotlist. The `asyncio.to_thread` calls are the highest-value instrumentation targets because they directly contribute to request tail latency on the Raspberry Pi deployment where the thread pool is small.

---

## 6. Open Decisions for the Operator

### (a) Export Target: Tempo vs File/Parquet for Ad-Hoc Analysis

> **DECISION (operator, 2026-05-30): Use Tempo via `docker-compose.monitoring.yml`.** This is the deployed compose file going forward; it starts Tempo always-on, which closes the dropped-traces gap described below. Action items: deploy with `docker-compose.monitoring.yml` (not the bare `with-monitoring` profile of `docker-compose.yml`); add a Tempo datasource to `ops/monitoring/grafana/provisioning/datasources/datasources.yml` for trace-to-log correlation; pin Tempo's WAL to the database data volume to limit SD-card wear. File/Parquet export remains the documented fallback only if Pi RAM becomes a binding constraint.

**Current state:** Applications export OTLP to `http://tempo:4317`. In `docker-compose.yml` with `--profile with-monitoring`, Tempo is not started, so traces are silently dropped unless `docker-compose.monitoring.yml` is used or a Tempo service block is added to `docker-compose.yml`.

**Options:**

| Option | Pros | Cons | Suitability for tens-of-requests/day |
|---|---|---|---|
| Tempo (already partly wired) | Native Grafana trace-to-log correlation via Loki; trace search via TraceQL; no export format change needed | Another service to operate; Tempo on Raspberry Pi consumes 100–300 MB RAM; S3/GCS backend adds complexity | Acceptable if `docker-compose.monitoring.yml` is the deployment file; not workable with the main compose file as-is |
| OTLP → file exporter + DuckDB/Polars | Zero runtime overhead; full trace JSON queryable offline with `duckdb`; trivial to archive; no additional container | No live Grafana trace panel; no real-time alerting on trace data | Well-suited for single-tenant, low-volume deployments where post-hoc analysis is acceptable |
| OTLP Collector → dual fan-out (Tempo + file) | Both live Grafana panels and offline analysis | OTel Collector is absent from all compose files; adds another container | Overkill for tens-of-requests/day |

**Recommendation for this deployment:** Use Tempo via `docker-compose.monitoring.yml`. The Raspberry Pi deployment already uses this file for its always-on stack. The only action required is to confirm Tempo is started alongside the application containers, which `docker-compose.monitoring.yml` already does. Allocate Tempo's WAL to the same volume as the database data to avoid SD card wear amplification. If RAM is a binding constraint (total container set on Pi approaches 1.5 GB), switch to the file exporter: add `OTLPFileExporter` targeting `/data/traces/` and schedule a weekly DuckDB query over the exported JSONL. At tens-of-requests/day the resulting file is under 10 MB per week.

### (b) Whether to Provision Grafana Dashboards

> **DECISION (operator, 2026-05-30): Provision per-phase dashboards plus one combined "Ratatoskr Operations" page.** Each phase ships its dashboard JSON under `ops/monitoring/grafana/provisioning/dashboards/` as part of that phase's PR. The combined page is the daily-driver; the per-phase dashboards serve debugging. Both are auto-loaded on Grafana startup.

**Grafana provisioning infrastructure already exists** at `ops/monitoring/grafana/provisioning/datasources/datasources.yml` and `ops/monitoring/grafana/provisioning/dashboards/`. Dashboard JSON files dropped into the dashboards provisioning directory are auto-loaded on Grafana startup.

**Recommendation:** Provision dashboards as part of each phase delivery. Each phase introduces at most one new dashboard or panel group.

| Phase | Dashboard |
|---|---|
| Phase 1 | Scraper chain: per-provider success rate (bar chart), per-provider latency P50/P95 (histogram panel), chain exhaustion rate over time |
| Phase 2 | LLM cost: tokens/request over time, cost_usd rolling 7-day, fallback rung distribution, retry exhaustion events |
| Phase 3 | Request funnel: interaction_type breakdown, cache hit rate, end-to-end latency P50/P95/P99 |
| Phase 4 | Agent latency: per-agent span duration, validation failure rate, repo analysis LLM calls per day |
| Phase 5 | Infrastructure: Qdrant operation latency, embedding latency, URL processor in-flight gauge, node-exporter CPU/RAM overlay |

A single-page "Ratatoskr Operations" dashboard combining the most actionable panels from all phases (scraper provider health, LLM cost, request latency, vector store health) delivers the highest daily-driver value. The per-phase dashboards serve for debugging.

Grafana Loki datasource is already provisioned; adding a Tempo datasource entry to `ops/monitoring/grafana/provisioning/datasources/datasources.yml` enables trace-to-log correlation for free once Tempo is receiving traces.
