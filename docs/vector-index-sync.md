# Vector-Index Sync: Fast Path + Reconciler

Ratatoskr keeps the Qdrant vector store in sync with Postgres `summaries` and analyzed GitHub `repositories` via two cooperating writers. Operational drift detection is handled by `VectorIndexReconciler`, which inspects each indexed entity type through a `VectorIndexedEntityAdapter` rather than hard-coding one query path per model.

## How it works

There are exactly two vector writers:

| Path | Latency | Scope | Notes |
|------|---------|-------|-------|
| **Fast path** (`persist` node, `app/infrastructure/vector/summary_point.py`) | ~instant (synchronous) | summaries | read-your-writes guarantee |
| **Taskiq reconciler** (`ratatoskr.vector.reconcile`) | 30 min cadence | summaries + repositories | steady-state convergence and backfill |

The fast path runs synchronously inside the summarize graph's `persist` node: it writes a byte-identical Qdrant point (via `app/infrastructure/vector/summary_point.py`) before the node returns, so a new summary is immediately retrievable for subsequent RAG grounding. If the fast-path write silently fails, the reconciler closes the gap on its next 30-minute run.

All summary writes produce the same Qdrant point UUID (`uuid5(NAMESPACE_OID, f"{request_id}:{summary_id}")`), so writes are idempotent. Repository vectors use a separate deterministic UUID: `uuid5(NAMESPACE_OID, f"{environment}:{user_scope}:repository:{repository_id}")`. Repository vectors are written by the GitHub analysis fast path (`RepositoryEmbeddingGenerator`) immediately after analysis, and reconciled by the Taskiq reconciler for backfill.

### Drift detection via `content_hash`

Every generated summary embedding stored in `summary_embeddings` stamps:

- `content_hash` — SHA256 of the text fed to the embedding model (computed by `SummaryEmbeddingGenerator`).
- `index_status` — set to `"pending"` until the Qdrant point write succeeds.
- `last_indexed_at` — UTC timestamp of the most recent successful Qdrant write.

On a subsequent generate call the generator short-circuits when an existing row's `content_hash` matches the freshly-prepared text — no embedding API call, no Qdrant upsert. The fast path and Taskiq reconciler mark rows `"indexed"` only after the matching Qdrant upsert succeeds. The reconciler treats `last_indexed_at < summaries.updated_at` (or NULL) as drift and re-runs the generator with `force=True`.

Repository embeddings use the same cursor: `repository_embeddings.content_hash` tracks the embedded repository text, `index_status` remains `"pending"` until Qdrant accepts the deterministic repository point, and `last_indexed_at` is updated only after that successful upsert.

## Environment variables

### Vector reconciler (Taskiq, on by default)

| Variable | Default | Description |
|----------|---------|-------------|
| `VECTOR_RECONCILE_ENABLED` | `true` | Enable the `ratatoskr.vector.reconcile` Taskiq job |
| `VECTOR_RECONCILE_CRON` | `*/30 * * * *` | Cron expression governing runs (UTC) |
| `VECTOR_RECONCILE_BATCH_SIZE` | `100` | Maximum stale summaries re-embedded per run |

The reconciler runs in the Taskiq worker process. The fast path ensures freshness for new summaries; the reconciler handles backfill and convergence for any rows the fast path missed.

## Prometheus metrics and alerts

The Taskiq reconciler emits these Prometheus series on every run:

| Metric | Type | Labels | Notes |
|--------|------|--------|-------|
| `ratatoskr_vector_reconcile_rows_total` | counter | `outcome` | Row outcomes for `scanned`, `requeued`, `skipped`, and `failed`. |
| `ratatoskr_vector_reconcile_oldest_lag_seconds` | gauge | none | Oldest lag among stale rows selected for the current run. For rows with no previous index timestamp, the summary `updated_at` timestamp is used as the lag marker. |
| `ratatoskr_vector_reconcile_runs_total` | counter | `status` | Run terminal status: `success` or `error`. Exceptions increment `status="error"` before being re-raised. |

`ops/monitoring/alerting_rules.yml` defines two reconciler alerts: `RatatoskrVectorReconcilerStalenessLagHigh` warns when stale-row lag exceeds two default `VECTOR_RECONCILE_CRON` periods, and `RatatoskrVectorReconcilerStopped` pages when no reconciler run status is observed for the same default two-period window.

## Reconciliation adapter seam

`app/infrastructure/vector/reconciliation.py` is the shared diagnostics and repair-inspection layer. The default reconciler is configured with two adapters:

- `SummaryVectorIndexedEntityAdapter` checks non-deleted `summaries`, `summary_embeddings`, model-version staleness, pending embeddings, and missing Qdrant summary points.
- `RepositoryVectorIndexedEntityAdapter` checks analyzed `repositories`, `repository_embeddings`, stale or pending repository embeddings, model-version staleness, and missing Qdrant repository points.

Each adapter returns `VectorIndexedEntityStats`; the reconciler aggregates them into `VectorReconciliationReport` and emits per-entity details under `details.entities`. The legacy top-level diagnostic fields (`expected_summaries`, `missing_summary_vectors`, `expected_repositories`, `missing_repository_vectors`, etc.) are preserved for dashboards, metrics, and existing tests. To add another vectorized entity type, implement `VectorIndexedEntityAdapter` and pass it via the `adapters=` constructor argument; do not fork the reconciler or add entity-specific branching to the report aggregation.

## Connection budget

Ratatoskr uses two (or three) Postgres connection pools simultaneously:

| Pool | Driver | Connections | Gating |
|------|--------|-------------|--------|
| SQLAlchemy (application) | asyncpg | `DB_POOL_SIZE` (default 5) | always on |
| LangGraph checkpointer | psycopg3 | min=1, **max=5** (ADR-0004) | `LANGGRAPH_CHECKPOINT_ENABLED=true` |

Total worst-case (both enabled): ~10 connections. Budget `max_connections` in Postgres accordingly (default 100 is fine).

The LangGraph checkpointer pool is a **separate dedicated psycopg3 `AsyncConnectionPool`** — distinct from the asyncpg `Database` pool. Its max size of 5 is the ADR-0004 authoritative value (`LANGGRAPH_CHECKPOINT_POOL_MAX_SIZE`). When `LANGGRAPH_CHECKPOINT_ENABLED=false` (the default), no psycopg3 pool is opened for the checkpointer and these connections are not consumed.

## CLI backfill

```bash
# Re-embed summaries and analyzed repositories that are missing or stale
python -m app.cli.backfill_vector_store --limit=100 --dry-run
python -m app.cli.backfill_vector_store

# Reconciler inspection (drift report without repairs)
python -m app.cli.reconcile_vector_index
```
