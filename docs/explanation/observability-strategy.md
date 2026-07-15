# Observability strategy

Ratatoskr combines durable PostgreSQL evidence, structured logs, Prometheus metrics, and optional OpenTelemetry traces. The correlation ID is the join key across those surfaces.

## Correlation-first diagnosis

Ingress assigns or preserves a correlation ID. Summary graph invocation uses it as the LangGraph thread ID, and downstream request/job rows, crawl attempts, LLM calls, progress events, logs, and user-visible failures retain it.

When a user reports `Error ID: <correlation_id>`:

1. locate the request and processing job in PostgreSQL;
2. inspect ordered crawl results and LLM attempts;
3. follow structured logs with the same ID;
4. if tracing is enabled, search Tempo by `ratatoskr.correlation_id`;
5. use the failing component's persisted error rather than rerunning blindly.

Example starting query:

```sql
SELECT id, status, source_kind, error_message, created_at, updated_at
FROM requests
WHERE correlation_id = '<error-id>';
```

Use the request ID to query `request_processing_jobs`, `crawl_results`, `llm_calls`, `progress_events`, and `summaries`. See [Troubleshooting](../reference/troubleshooting.md) and the `inspecting-database` skill for maintained queries.

## Structured logging

Logging helpers live in `app/core/logging_utils.py`. Services attach compact context such as `cid`, `uid`, request/entity IDs, provider, outcome, and duration. Authorization headers, tokens, cookies, and unnecessary payload contents must be redacted before logging.

`DEBUG_PAYLOADS` may increase diagnostic detail, but it does not authorize secret logging. Prefer bounded previews and persisted artifacts with explicit retention over full payloads in log streams.

## Metrics

Prometheus instruments application behavior through `app/observability/metrics.py` and subsystem-specific metric helpers. Metrics cover request outcomes/latency, extraction providers, LLM attempts and costs, Taskiq retries/failures, vector reconciliation, social integrations, and operational maintenance.

Use metrics to detect rates and trends; use correlation-linked records to explain one failure. Avoid labels containing raw URLs, prompts, user text, tokens, or unbounded IDs.

The `with-monitoring` Compose profile supplies Prometheus, Alertmanager, Grafana, Loki, Promtail, node-exporter, and tracing services declared by the current Compose file.

## OpenTelemetry

Tracing is opt-in through `OTEL_ENABLED`. Provider/exporter setup is implemented in `app/observability/otel.py`; stable attribute names live in `app/observability/attributes.py`. The configuration owner and validation rules are documented in [Environment Variables](../reference/environment-variables.md#configuration-ownership).

Instrumented boundaries include HTTP/Telegram ingress, summary graph nodes, scraper providers, LLM calls, database sessions/transactions, Taskiq propagation, Redis, vector/embedding operations, and selected application use cases. Traces must not contain secret or full-content payloads.

## Durable audit trail

PostgreSQL stores the evidence needed to reconstruct processing: message snapshots, request/job state, crawl results, LLM calls, summaries, audit logs, and task failure records. Raw-data retention settings may redact or purge payload bodies while preserving status, cost, timing, and summary/search metadata.

Qdrant and Redis are not audit stores. Qdrant can be reconciled from authoritative records for supported entities; Redis state is ephemeral in the default deployment.

## Operational rules

- Every user-visible failure includes its Error ID.
- Retries keep the original correlation context and increment persisted attempt metadata.
- Alerts identify a symptom and link to the first diagnostic surface; they do not embed sensitive payloads.
- Health endpoints report reachability/readiness, not proof that every optional provider works.
- A successful metric or span never substitutes for checking durable terminal state.

Last audited: 2026-07-15.
