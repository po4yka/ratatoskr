---
title: Implement phased pipeline observability instrumentation plan
status: backlog
area: observability
priority: high
owner: unassigned
blocks: []
blocked_by: []
created: 2026-05-30
updated: 2026-05-30
---

- [ ] #task Implement phased pipeline observability instrumentation plan #repo/ratatoskr #area/observability #status/backlog ⏫

## Objective

Close the end-to-end performance-analytics gap across every pipeline stage (Telegram intake -> scraper chain -> LLM -> validation -> embedding -> delivery) so the operator can answer: where wall-clock time is lost, which scraper providers burn timeouts for nothing, and what LLM calls cost per model. Single-tenant, ~tens of requests/day; high cardinality is acceptable.

The full read-only audit and phased plan live at `docs/explanation/observability-instrumentation-plan.md`. This issue tracks executing that plan. **Do not re-derive the gaps** — the plan already contains verified `file:line` insertion points.

## Context (from the audit)

The naive assumption "no telemetry exists" is wrong. The audit found:

- **OTel SDK is wired and operational** via `init_tracing()` at five entry-points (`bot.py:25`, `app/api/main.py:96`, `app/tasks/broker.py:22`, `app/tasks/scheduler.py:15`, `app/mcp/server.py:112`); idempotent guard at `app/observability/otel.py:74`. OTLP exporter targets `OTEL_EXPORTER_OTLP_ENDPOINT` (default `http://tempo:4317`). HTTPX/Redis/logging auto-instrumented.
- **A large Prometheus helper module** (`app/observability/metrics.py`, isolated `REGISTRY`) with ~40 `record_*` helpers already exists. The work is mostly **wiring existing helpers to call sites**, not defining new metrics.
- **Two dead-export helpers**: `record_scraper_attempt` (`metrics.py:1075`) and `record_scraper_attempt_latency` (`metrics.py:1089`) have zero call sites. `record_llm_call_retry_exhaustion` (`metrics.py:993`) is also never called.
- **Monitoring stack deployed**: Prometheus, Grafana, Loki, Promtail, node-exporter are real (compose profile `with-monitoring` in `docker-compose.yml`; always-on in `docker-compose.monitoring.yml`). **Tempo is only in `docker-compose.monitoring.yml`** — under the main compose `with-monitoring` profile, apps emit OTLP to a `tempo:4317` host that does not exist, so **traces are silently dropped**. No OTel Collector anywhere.
- **Existing convention** (see `add-channel-digest-metrics-and-alerts.md`): metrics registered in `app/observability/` + alert rules in `ops/monitoring/alerting_rules.yml` + Grafana provisioning in `ops/monitoring/grafana/provisioning/`.

## Scope (phased, ranked by analytics value)

Each phase is independently deployable. Exact `file:line` insertion points, attribute keys, and the per-point analytics question are in the plan doc.

- **Phase 1 - Scraper per-rung Prometheus metrics.** Wire the two dead-export helpers at the existing `_attempt_provider` outcome sites in `app/adapters/content/scraper/chain.py` (`:479`, `:494`, `:562`, `:565`) and enrich the existing `scraper.<name>` span at `chain.py:468`. Do NOT add per-provider inner spans — the chain already opens one, so inner spans would nest-duplicate.
- **Phase 2 - LLM token/cost metrics + span attributes.** Enrich `llm.chat` span at `openrouter_client.py:534` (tokens, model_served); restructure the bare `return await` for `llm.chat_structured` (`:563`) before attributes can be set; wire `record_llm_call_retry_exhaustion` at `chat_engine.py:419`; add fallback-rung attributes at `chat_engine.py:346`.
- **Phase 3 - Request root span + correlation_id propagation + source_type.** Enrich `telegram.update` span at `message_router.py:161`; call `set_correlation_id_attr` inside `url_flow.process` at `url_processor.py:232`; add `url_flow.cache_hit` span event at `cached_summary_responder.py:54`.
- **Phase 4 - Agents + self-correction retries.** Add `agent.<name>` spans across `app/agents/` (zero spans today); wire LLM metrics in `web_search_agent.py:205` and via `record_llm_call_persisted` at `repo_analysis_agent.py:367`.
- **Phase 5 - Embedding/Qdrant + concurrency gauges.** Qdrant op latency/success via `record_db_query`/`record_vector_write` at `qdrant_store.py:348/350/397/455`; embedding latency at `embedding_service.py:115/129` and `gemini_embedding_service.py:135`; new `ratatoskr_url_processor_in_flight` gauge at `url_processor.py:230`/`:543`.
- **Cross-cutting - unified attribute namespace.** New `app/observability/attributes.py` constants module (`ratatoskr.*` keys grouped by stage). Migrate existing inline span-attribute strings (`chain.py:225`, `openrouter_client.py:511`) in a separate PR to avoid Grafana span-query churn.
- **Blocking-call hotlist.** Instrument/verify the `asyncio.to_thread` and sync-load hotspots: `embedding_service.py:84/115/129`, `gemini_embedding_service.py:135`, `youtube/download_pipeline.py:99-100`, and verify the transcript-API call chain (`transcript_api.py`) is off-loop before adding a span.

## Open decisions (operator) — RESOLVED 2026-05-30

Both decisions are settled; recorded in `docs/explanation/observability-instrumentation-plan.md` §6.

1. **Trace export target → Tempo via `docker-compose.monitoring.yml`.** This becomes the deployed compose file (Tempo always-on), closing the dropped-traces gap. Add a Tempo datasource to `ops/monitoring/grafana/provisioning/datasources/datasources.yml`; pin Tempo WAL to the DB data volume. File/Parquet export is fallback-only if Pi RAM is constrained.
2. **Grafana dashboards → provision per-phase + one combined "Ratatoskr Operations" page.** Each phase ships its dashboard JSON under `ops/monitoring/grafana/provisioning/dashboards/` in that phase's PR; the combined page is the daily-driver.

## Acceptance criteria

- [x] Operator decisions (1) and (2) recorded in the plan doc (§6) — resolved 2026-05-30.
- [ ] Tempo dropped-traces gap resolved: deploy via `docker-compose.monitoring.yml` so Tempo is reachable at `tempo:4317` — otherwise all span work is invisible.
- [ ] Each phase: insertion points from the plan implemented at the verified `file:line`, reusing existing `app/observability/` helpers (no duplicate metric families except the explicitly-new `ratatoskr_url_processor_in_flight` gauge and any agent validation-failure counter).
- [ ] No nested-span duplication introduced in the scraper chain (Phase 1 enriches the existing rung span).
- [ ] Unit tests assert each newly-wired counter/histogram increments on a forced scenario (scraper success/timeout/no-content, LLM token record, retry exhaustion, Qdrant op, embedding encode).
- [ ] `ratatoskr.*` attribute keys reference `app/observability/attributes.py` constants, not inline strings, for all newly-added attributes.
- [ ] If dashboards are approved: dashboard JSON committed under `ops/monitoring/grafana/provisioning/dashboards/`.

## References

- Full plan: `docs/explanation/observability-instrumentation-plan.md`
- OTel SDK: `app/observability/otel.py` (`init_tracing` `:67`, `get_tracer` `:130`, `set_correlation_id_attr` `:137`)
- Metrics helpers: `app/observability/metrics.py` (dead exports at `:1075`, `:1089`, `:993`)
- Compose / monitoring: `ops/docker/docker-compose.yml` (profile `with-monitoring`), `ops/docker/docker-compose.monitoring.yml`, `ops/monitoring/`
- Prior-art issue (same convention): `add-channel-digest-metrics-and-alerts.md`
