# Vector Reconcile Runbook

Use this when semantic search returns stale/missing results, Qdrant is unavailable, repository vectors drift, or the Taskiq vector reconciler backlog grows.

## Symptoms

- Alert `RatatoskrVectorReconcilerStalenessLagHigh` or `RatatoskrVectorReconcilerStopped` fires.
- Semantic search, related reads, RAG grounding, MCP vector tools, or repository search miss newly-created summaries/repositories.
- Logs contain `ratatoskr.vector.reconcile`, `vector_reconcile`, `qdrant`, `embedding`, `last_indexed_at`, or Taskiq retry/dead-letter events for `ratatoskr.vector.reconcile`.
- Qdrant collection point count diverges materially from Postgres `summary_embeddings` plus `repository_embeddings`.
- `summary_embeddings.last_indexed_at` or `repository_embeddings.last_indexed_at` is null/stale for rows that should already be searchable.

## Log Queries

```bash
docker compose -f ops/docker/docker-compose.yml logs --tail=300 worker ratatoskr | rg 'vector|reconcile|qdrant|embedding|ratatoskr.vector.reconcile'
curl -fsS http://localhost:6333/collections | jq .
curl -fsS http://localhost:6333/collections/ratatoskr | jq .
```

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c "SELECT count(*) FILTER (WHERE se.last_indexed_at IS NULL OR se.last_indexed_at < s.updated_at) AS stale, count(*) AS total FROM summary_embeddings se JOIN summaries s ON s.id = se.summary_id;"
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c "SELECT s.id, s.updated_at, se.last_indexed_at FROM summaries s LEFT JOIN summary_embeddings se ON se.summary_id = s.id WHERE se.last_indexed_at IS NULL OR se.last_indexed_at < s.updated_at ORDER BY s.updated_at DESC LIMIT 20;"
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c "SELECT count(*) FILTER (WHERE re.last_indexed_at IS NULL OR re.last_indexed_at < r.updated_at) AS stale_repos, count(*) AS total_repos FROM repository_embeddings re JOIN repositories r ON r.id = re.repository_id;"
```

## Prometheus Panels

- Alerts: `RatatoskrVectorReconcilerStalenessLagHigh`, `RatatoskrVectorReconcilerStopped`, and `RatatoskrTaskiqDeadLettersHigh` when the reconciler dead-letters.
- Grafana: `Ratatoskr Overview` (`ratatoskr-overview`) panels `Request Rate by Status`, `Database Query Latency`, `Circuit Breaker State History`, and `Error Rate (5m)`.
- Metrics to query directly: `ratatoskr_vector_reconcile_oldest_lag_seconds`, `ratatoskr_vector_reconcile_runs_total`, and Taskiq retry metrics filtered to `task="ratatoskr.vector.reconcile"`.

## Mitigation Steps

1. Verify Qdrant health first: `curl -fsS http://localhost:6333/collections`; if Qdrant is down, restart it with `docker compose -f ops/docker/docker-compose.yml restart qdrant` and wait for collections to load.
2. Verify the worker and scheduler are running: `docker compose -f ops/docker/docker-compose.yml ps worker scheduler`; restart `worker` if no reconciler logs appear after one cron interval.
3. Force one reconciler run: `python -c "import asyncio; from app.tasks.reconcile_vector_index import reconcile_vector_index; asyncio.run(reconcile_vector_index.kiq())"` and watch worker logs.
4. If the backlog is large but shrinking, leave the reconciler running; avoid full backfill because it competes with normal summarization and embedding generation.
5. If Qdrant was recreated or the embedding provider changed, run `python -m app.cli.backfill_vector_store --force` for summaries and `python -m app.cli.backfill_repository_embeddings` for repository embeddings, then run the reconciler once.
6. If only a small subset is stale, prefer scoped backfill or reconciler repair over deleting the whole collection; keep deterministic point IDs intact.
7. Confirm recovery by comparing Postgres stale counts, Qdrant `points_count`, and one semantic search query for a known recent summary/repository.

## Escalation

Page the maintainer if Qdrant cannot load its collection, embedding dimensions mismatch after provider switch, reconciler rows dead-letter repeatedly, or semantic search remains incorrect after a successful backfill/reconcile run.

## References

- `docs/vector-index-sync.md`
- `.codex/skills/vector-index-sync/SKILL.md`
- `app/tasks/reconcile_vector_index.py`
- `app/cli/backfill_vector_store.py`
