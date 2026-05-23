# Vector-Index Sync: CocoIndex + Reconciler

Ratatoskr keeps the Qdrant vector store in sync with Postgres `summaries` and analyzed GitHub `repositories`. Summary vectors still have three cooperating paths. Repository vectors are written immediately by the GitHub analysis fast path and reconciled by CocoIndex when the live updater is enabled. Operational drift checks are handled by `VectorIndexReconciler`, which inspects each indexed entity type through a `VectorIndexedEntityAdapter` rather than hard-coding one query path per model.

## Quick start

```bash
# Install the extra
pip install -e ".[cocoindex]"

# Enable in .env
RATATOSKR_COCOINDEX_ENABLED=1
```

## How it works

Summary vectors have three writers:

| Path | Latency | Owner |
|------|---------|-------|
| **Fast path** (`SummaryEmbeddingGenerator`) | ~instant | write-through, best-effort |
| **CocoIndex flow** (`FlowLiveUpdater`, opt-in) | ≤30s | authoritative when enabled |
| **Taskiq reconciler** (`ratatoskr.vector.reconcile`) | 30 min | steady-state fallback |

All summary paths produce the same Qdrant point UUID (`uuid5(NAMESPACE_OID, f"{request_id}:{summary_id}")`), so writes are idempotent. The fast path can silently lose a write; CocoIndex reconciles within one poll interval, and the Taskiq reconciler closes the gap when CocoIndex is disabled.

Repository vectors have two writers: the GitHub analysis fast path (`RepositoryEmbeddingGenerator`) for immediate search freshness, and the CocoIndex repository flow for live or one-shot reconciliation of analyzed rows. They use a separate deterministic UUID: `uuid5(NAMESPACE_OID, f"{environment}:{user_scope}:repository:{repository_id}")`. The repository CocoIndex flow only exports rows that already have `analysis_json`; LLM analysis, budget caps, and pending-analysis flags remain owned by the GitHub ingestion workflow.

### Drift detection via `content_hash`

Every successful write to `summary_embeddings` stamps:

- `content_hash` — SHA256 of the text fed to the embedding model (computed by `SummaryEmbeddingGenerator`).
- `last_indexed_at` — UTC timestamp of the write.
- `index_status` — set to `"indexed"`.

On a subsequent generate call the generator short-circuits when an existing row's `content_hash` matches the freshly-prepared text — no embedding API call, no Qdrant upsert. The reconciler treats `last_indexed_at < summaries.updated_at` (or NULL) as drift and re-runs the generator with `force=True`.

## Environment variables

### CocoIndex live updater (opt-in)

| Variable | Default | Description |
|----------|---------|-------------|
| `RATATOSKR_COCOINDEX_ENABLED` | `0` | Enable CocoIndex (set to `1` to activate) |
| `RATATOSKR_COCOINDEX_DSN` | *(DATABASE_URL)* | Override Postgres DSN for CocoIndex (strips asyncpg prefix automatically) |
| `RATATOSKR_COCOINDEX_POLL_INTERVAL_SEC` | `30` | Seconds between watermark polls when LISTEN/NOTIFY is idle |
| `RATATOSKR_COCOINDEX_LISTEN_CHANNEL` | `ratatoskr_summaries_changed` | Postgres LISTEN/NOTIFY channel |
| `RATATOSKR_COCOINDEX_BATCH_SIZE` | `32` | Rows per processing batch |
| `RATATOSKR_COCOINDEX_POOL_MAX` | `4` | Max psycopg3 connections |

### Vector reconciler (Taskiq, on by default)

| Variable | Default | Description |
|----------|---------|-------------|
| `VECTOR_RECONCILE_ENABLED` | `true` | Enable the periodic reconciler |
| `VECTOR_RECONCILE_CRON` | `*/30 * * * *` | Cron expression governing runs (UTC) |
| `VECTOR_RECONCILE_BATCH_SIZE` | `100` | Maximum stale summaries re-embedded per run |

The reconciler runs in the Taskiq worker process. When CocoIndex is enabled the two paths overlap harmlessly — both produce the same UUIDs. To rely solely on CocoIndex, set `VECTOR_RECONCILE_ENABLED=false`.

## Reconciliation adapter seam

`app/infrastructure/vector/reconciliation.py` is the shared diagnostics and repair-inspection layer. The default reconciler is configured with two adapters:

- `SummaryVectorIndexedEntityAdapter` checks non-deleted `summaries`, `summary_embeddings`, model-version staleness, pending embeddings, and missing Qdrant summary points.
- `RepositoryVectorIndexedEntityAdapter` checks analyzed `repositories`, `repository_embeddings`, model-version staleness, pending analysis, and missing Qdrant repository points.

Each adapter returns `VectorIndexedEntityStats`; the reconciler aggregates them into `VectorReconciliationReport` and emits per-entity details under `details.entities`. The legacy top-level diagnostic fields (`expected_summaries`, `missing_summary_vectors`, `expected_repositories`, `missing_repository_vectors`, etc.) are preserved for dashboards, metrics, and existing tests. To add another vectorized entity type, implement `VectorIndexedEntityAdapter` and pass it via the `adapters=` constructor argument; do not fork the reconciler or add entity-specific branching to the report aggregation.

## Connection budget

Ratatoskr uses three Postgres connection pools simultaneously when CocoIndex and LangGraph checkpointing are both enabled:

| Pool | Driver | Connections |
|------|--------|-------------|
| SQLAlchemy (application) | asyncpg | `DB_POOL_SIZE` (default 5) |
| LangGraph checkpointer | psycopg3 | min=1, max=10 |
| CocoIndex flows | psycopg3 | max=4 + 1 (LISTEN/NOTIFY) |

Total worst-case: ~20 connections. Budget `max_connections` in Postgres accordingly (default 100 is fine; set `RATATOSKR_COCOINDEX_POOL_MAX=2` to reduce if needed).

## Startup failure isolation

CocoIndex startup errors are caught and logged (`cocoindex_startup_failed`) without blocking FastAPI from serving requests. If CocoIndex fails to start, the fast path continues as the sole writer to Qdrant.

## Rollback

1. Set `RATATOSKR_COCOINDEX_ENABLED=0` in `.env`
2. Redeploy — FastAPI starts without CocoIndex
3. The Taskiq reconciler (`ratatoskr.vector.reconcile`) keeps Qdrant converged on its 30-minute cadence; existing fast path + CLI backfill continue working unchanged

## CLI backfill with CocoIndex

```bash
# Use CocoIndex for a one-shot full-scan of summaries and analyzed repositories
python -m app.cli.backfill_vector_store --use-cocoindex

# Legacy backfill (still works, default when --use-cocoindex is absent)
python -m app.cli.backfill_vector_store --limit=100 --dry-run
```

## v1 limitations

- **One point per entity** — CocoIndex v1 emits a single Qdrant point per summary or repository, unlike the legacy summary backfill which emits chunked window points. This is a deliberate simplification; retrieval quality is measured before adding chunked points in a follow-up.
- **Alpha stability** — CocoIndex is pinned to `>=1.0.3,<1.1`. Pin review when 1.1 releases.
- **Trigger creation** — Requires the `ratatoskr` Postgres role to have `TRIGGER` privilege on `summaries` and `repositories`. Migrations 0007 and 0008 grant this; verify the role name matches your deployment.

## CocoIndex bookkeeping schema

CocoIndex stores its watermark and metadata tables in a dedicated `cocoindex` Postgres schema (created by migration 0007). These tables are managed entirely by CocoIndex and should not be modified manually.
