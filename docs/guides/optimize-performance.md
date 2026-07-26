# Optimize performance and cost

Tune Ratatoskr from observed bottlenecks, not fixed model rankings or historic provider prices. Start with request/attempt timings, Taskiq capacity, database health, and vector lag.

## Establish a baseline

For representative URLs, record:

- end-to-end and per-graph-node latency;
- scraper winner and number/duration of failed attempts;
- LLM model attempts, tokens, latency, and persisted cost metadata;
- Taskiq queue/retry/failure behavior;
- PostgreSQL slow queries/pool pressure;
- Qdrant reconciliation lag and search latency.

Use correlation IDs to inspect individual outliers and metrics for aggregate trends.

## Extraction

Keep deterministic/in-process providers ahead of browser or LLM-driven providers unless host-specific evidence shows a better order. The default 13-position chain already skips unsupported/disabled rungs. Configure JS-heavy hosts explicitly and keep Webwright double-gated by feature flag and host allowlist.

Relevant settings are under `scraper` in `ratatoskr.yaml` and in [Environment Variables](../reference/environment-variables.md#scraping-and-search).

## LLM

Choose the provider/model from current quality, latency, context, structured-output, compliance, and price measurements. Avoid documentation-pinned claims about the “fastest” or “cheapest” model. Tune:

- primary and fallback model order;
- per-model and total call budgets;
- concurrency caps;
- prompt caching where supported;
- optional enrichment/two-pass features;
- max response and context sizes.

Never reduce summary validation or repair safety solely to improve throughput. See [LLM Providers](../reference/llm-providers.md).

## Taskiq and concurrency

The worker launcher derives process count and per-process async capacity from current configuration. Increasing processes multiplies database connections, external-call concurrency, memory, and model instances. Review the startup capacity summary and connection budget before scaling.

Scale the singleton scheduler separately from workers. Preserve distributed locks for tasks that must not overlap.

## PostgreSQL

Use PostgreSQL query plans and table/index statistics before adding indexes. Keep the configured async pool within the server connection budget across bot, API, workers, and checkpointing. Apply schema/index changes through Alembic and verify representative queries.

## Search and Qdrant

PostgreSQL full-text search is available without vector generation. For semantic/hybrid search, keep embedding dimensions/space consistent and monitor reconciliation rather than rebuilding on every anomaly:

```bash
python -m app.cli.reconcile_vector_index
python -m app.cli.reconcile_vector_index --repair
```

Use `--dry-run` and `--limit=N` when bounding a repair. See [Vector Index Synchronization](../vector-index-sync.md).

## Redis caches

Tune only settings implemented by `RedisConfig`. `REDIS_ENABLED` controls the shared client and `REDIS_CACHE_ENABLED` controls the generic JSON cache. Keep `REDIS_CACHE_TIMEOUT_SEC` below the request latency budget, and adjust the cache-specific TTLs only after measuring reuse:

- `REDIS_FIRECRAWL_TTL_SECONDS` and `REDIS_LLM_TTL_SECONDS` for extraction and summary results;
- `REDIS_TRENDING_CACHE_TTL_SECONDS` for trending topics;
- `REDIS_AUTH_TOKEN_CACHE_TTL_SECONDS` for cached authentication decisions;
- `REDIS_BATCH_PROGRESS_TTL_SECONDS` for batch progress;
- `REDIS_EMBEDDING_CACHE_TTL_SECONDS` for embeddings.

Connection location and retry behavior are controlled by `REDIS_URL` (or `REDIS_HOST`, `REDIS_PORT`, and `REDIS_DB`), `REDIS_SOCKET_TIMEOUT`, and `REDIS_RECONNECT_INTERVAL_SEC`. `REDIS_REQUIRED` sets the deployment availability policy; generic JSON cache reads and writes remain fail-open. Token usage comes from persisted provider responses; it has no independent cache-tuning mode.

Use the Redis Cache row on the provisioned Ratatoskr Overview dashboard to compare hit ratio, errors, and p95 operation latency by bounded cache namespace. The exported `ratatoskr_redis_cache_operations_total` and `ratatoskr_redis_cache_operation_latency_seconds` metrics never include Redis keys or user identifiers.

## Storage and retention

Downloaded videos, attachments, raw crawl/LLM payloads, browser trajectories, backups, and vectors have different retention value. Configure subsystem retention and backup policies; do not delete authoritative PostgreSQL data to save cache space. Redis is ephemeral in the default deployment.

## Validation after tuning

Repeat the same workload and compare outcome quality as well as latency/cost. Exercise fallback and timeout paths, confirm correlation/persistence remains intact, and watch the deployment long enough to observe scheduled tasks and pool pressure.

Last audited: 2026-07-15.
