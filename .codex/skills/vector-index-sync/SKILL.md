---
name: vector-index-sync
description: Understand and debug the two-writer vector index sync between Postgres summaries/repositories and the Qdrant vector store. Trigger keywords -- vector, Qdrant, embeddings, reconciler, last_indexed_at, point_ids, semantic search, vector reconcile.
version: 1.1.0
allowed-tools: Bash, Read, Grep
---

# Vector Index Sync

Summary and repository embeddings live in two stores that must stay consistent: relational rows in Postgres (`summary_embeddings`, `repository_embeddings`) and vector points in Qdrant. Two cooperating writers keep them aligned.

## The Two Writers

| Writer | Where | When |
| ------ | ----- | ---- |
| **Fast path** | `persist` graph node → `SummaryEmbeddingGenerator` (summaries), GitHub repo analysis (repos) | Synchronously on summary creation / repo analysis -- the user gets a fresh vector immediately (read-your-writes guarantee for RAG grounding) |
| **Taskiq reconciler** | `app/tasks/reconcile_vector_index.py` (`ratatoskr.vector.reconcile`) | Cron `VECTOR_RECONCILE_CRON` (default `*/30 * * * *`); scans rows where `last_indexed_at < summaries.updated_at` |

The reconciler is the convergence/backfill/repair layer. The fast path covers steady-state writes and guarantees the new summary is retrievable immediately. Both writers produce byte-identical Qdrant points via `app/infrastructure/vector/summary_point.py` and shared deterministic point IDs.

## Deterministic Point IDs

Defined in `app/infrastructure/vector/point_ids.py`. The same summary always maps to the same Qdrant point ID across both writers, so they don't fight each other.

Repository points live in the SAME collection as summary points, discriminated by `entity_type="repository"` in the payload.

## Dynamic Context

```bash
!docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -t -c "SELECT count(*) FILTER (WHERE last_indexed_at IS NULL OR last_indexed_at < s.updated_at) AS stale, count(*) AS total FROM summary_embeddings se JOIN summaries s ON s.id = se.summary_id"
```

## Common Queries

### Stale summary vectors (the reconciler's input)

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT s.id, s.updated_at, se.last_indexed_at
     FROM summaries s LEFT JOIN summary_embeddings se ON se.summary_id = s.id
    WHERE se.last_indexed_at IS NULL OR se.last_indexed_at < s.updated_at
    ORDER BY s.updated_at DESC LIMIT 20;"
```

### How many points in Qdrant?

```bash
# Hit Qdrant's HTTP API; adjust port if non-default
curl -s "http://localhost:6333/collections/ratatoskr" | python -m json.tool
```

Compare `points_count` with the Postgres `summary_embeddings` count -- a divergence means a writer is failing.

### Force a reconciler run

```bash
# Taskiq task can be invoked manually
python -c "import asyncio; from app.tasks.reconcile_vector_index import reconcile_vector_index; asyncio.run(reconcile_vector_index.kiq())"
```

Or wait for the next `VECTOR_RECONCILE_CRON` tick.

### Backfill from scratch

```bash
python -m app.cli.backfill_vector_store --help
```

This re-embeds and re-upserts everything. Slow but authoritative.

## Embedding Provider Switching

`EmbeddingConfig` (see `app/infrastructure/embedding/embedding_factory.py`) picks the provider:

| Provider | Env | Notes |
| -------- | --- | ----- |
| `local` | `EMBEDDING_PROVIDER=local` (default) | `sentence-transformers`, runs in-process |
| `gemini` | `EMBEDDING_PROVIDER=gemini` + `GEMINI_API_KEY` | Google's Gemini Embedding 2 API |
| `voyage` | `EMBEDDING_PROVIDER=voyage` + `VOYAGE_API_KEY` | Voyage AI text embeddings via direct HTTP |

**Switching providers invalidates ALL existing vectors** -- the embedding dimensions and semantics differ. Remote providers are namespaced by model + dimension; run the backfill after switching and use a fresh collection/version or recreate the incompatible collection first.

## Failure Modes

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Search returns stale results after a summary edit | Reconciler hasn't run yet (~30 min lag) | Wait, or invoke `reconcile_vector_index` manually |
| Qdrant point count != Postgres row count | One writer is erroring silently | Check logs for `vector.reconcile` errors |
| Search returns nothing | Collection dimension mismatch after provider switch | Run `backfill_vector_store` |

## Key Files

- **Fast path (summaries)**: `app/infrastructure/embedding/summary_embedding_generator.py`
- **Fast path (repos)**: `app/agents/repo_analysis_agent.py`, `app/application/use_cases/analyze_repository.py`
- **Summary point construction**: `app/infrastructure/vector/summary_point.py`
- **Reconciler task**: `app/tasks/reconcile_vector_index.py`
- **Point IDs**: `app/infrastructure/vector/point_ids.py`
- **Qdrant store**: `app/infrastructure/vector/qdrant_store.py`
- **Embedding factory**: `app/infrastructure/embedding/embedding_factory.py`
- **CLI backfill**: `app/cli/backfill_vector_store.py`

## Important Notes

- Both writers MUST go through `point_ids.py` -- never compute Qdrant IDs ad-hoc.
- `last_indexed_at` is the reconciliation cursor -- writers must update it after a successful Qdrant upsert.
- Repository and summary points share one Qdrant collection; always include `entity_type` in queries.
- `VECTOR_RECONCILE_ENABLED=true` is the default; disabling it means stale vectors will accumulate.
- The reconciler scans in batches of `VECTOR_RECONCILE_BATCH_SIZE` (default 100) per run -- adjust for large backlogs.
