# Troubleshooting

Start with the user-visible Error ID. It is the correlation key for logs, request/job state, crawl attempts, LLM calls, progress events, and summaries. Preserve the first failure before retrying or changing state.

## First response

```bash
docker compose -f ops/docker/docker-compose.yml ps
docker compose -f ops/docker/docker-compose.yml logs --tail=200 ratatoskr worker scheduler mobile-api
curl -fsS http://127.0.0.1:18000/health/ready
```

Check the schema without changing it:

```bash
docker compose -f ops/docker/docker-compose.yml exec ratatoskr \
  python -m app.cli.migrate_db --check
```

Do not paste `.env`, tokens, cookies, authorization headers, raw private content, or encryption keys into diagnostics.

## Configuration Issues

Validate the merged configuration in the same environment as the failing service:

```bash
docker compose -f ops/docker/docker-compose.yml exec ratatoskr \
  python -c "from app.config import load_config; load_config(); print('configuration valid')"
```

Common causes:

- missing Telegram, PostgreSQL, or selected-provider secrets;
- model fields absent from `ratatoskr.yaml` for the selected provider;
- a secret incorrectly placed in YAML (secret-marked YAML keys are ignored);
- a deprecated environment variable rejected at startup;
- production using a baked YAML file different from the operator's expected file;
- app services unable to resolve `postgres`, `redis`, or `qdrant` outside Compose.

Compare [Environment Variables](environment-variables.md), [YAML Configuration](config-file.md), and the service's actual Compose environment.

## Request Stuck in Processing

The sole summary path starts at `GraphURLProcessor.handle_url_flow` and runs `app/application/graphs/summarize/`. Durable jobs live in `request_processing_jobs` with leases/fencing tokens.

For the correlation ID, inspect:

```sql
SELECT id, status, error_message, created_at, updated_at
FROM requests
WHERE correlation_id = '<error-id>';

SELECT request_id, status, lease_owner, lease_expires_at, lease_token,
       attempt_count, last_error, updated_at
FROM request_processing_jobs
WHERE request_id = <request-id>;
```

Then inspect the latest `progress_events`, `crawl_results`, and `llm_calls`. A live unexpired lease should not be stolen manually. Use the maintained retry/requeue command only after confirming the worker that owns the lease is gone and the job is eligible.

Check worker imports and queue connectivity through the `worker` logs; scheduled jobs require both the singleton scheduler and at least one worker.

## Content Extraction Failures

Generic URLs use the 13-provider chain in `app/adapters/content/scraper/`; YouTube, Twitter/X, and academic papers use dedicated extractors. Inspect `crawl_results` in attempt order and distinguish:

- unsupported/skipped provider;
- network/sidecar failure;
- SSRF or host-allowlist rejection;
- successful transport but content below the quality threshold;
- platform extractor failure before the generic chain;
- total chain failure.

Check optional sidecars only when their profile is enabled:

```bash
curl -fsS http://127.0.0.1:3002/serverHealthCheck
curl -fsS http://127.0.0.1:11235/health
curl -fsS http://127.0.0.1:3003/health
```

Use [Scraper Chain](../explanation/scraper-chain.md) and the `scraper-chain-debugging` skill for provider-specific evidence. Do not disable SSRF, allowlist, or content-quality checks to force success.

## OpenRouter Issues

OpenRouter is the default LLM path; direct OpenAI, Anthropic, and Ollama adapters have their own credentials/model namespaces. For any provider, inspect ordered `llm_calls` for model, trigger/index, status, latency, token usage, sanitized error, and response-format attempt.

Typical causes:

- wrong provider key or model namespace;
- exhausted account/rate limit;
- per-model or overall timeout;
- unsupported structured-output mode;
- invalid JSON/contract after all bounded repair attempts;
- fallback list containing unavailable or incompatible models.

Verify provider reachability outside the workflow only with a minimal sanitized request and the exact selected model. Use [LLM Providers](llm-providers.md) and the `debugging-apis` skill.

## JSON Parsing Failures

Validation and repair are graph nodes in `app/application/graphs/summarize/nodes/validate.py` and `repair.py`. The accepted shape is owned by `app/core/summary_contract.py` and `app/core/summary_schema.py`.

Inspect every `llm_calls` attempt rather than only the final error. Confirm:

- provider response mode matches the descriptor;
- the raw response was persisted/sanitized as configured;
- validation errors are specific and repeatable;
- repair attempts are bounded by graph state;
- EN/RU prompt variants and descriptor schema are in sync.

Do not hand-fill required fields or loosen the contract to accept malformed output. Reproduce with the same content/model through a targeted test or CLI run, then fix prompt/provider compatibility or shaping logic.

## Database Issues

PostgreSQL is authoritative. Check reachability and Alembic state:

```bash
docker compose -f ops/docker/docker-compose.yml exec postgres \
  pg_isready -U ratatoskr_app -d ratatoskr
docker compose -f ops/docker/docker-compose.yml exec ratatoskr \
  python -m app.cli.migrate_db --status
```

The migration CLI renders SQL by default. Apply reviewed revisions explicitly:

```bash
docker compose -f ops/docker/docker-compose.yml run --rm migrate \
  python -m app.cli.migrate_db --apply
```

Do not drop/recreate a production database as a diagnostic shortcut. Preserve the failing database and restore only from a verified backup through [Back Up and Restore](../guides/backup-and-restore.md).

## Redis Issues

Redis backs Taskiq, distributed locks, rate limits, sync sessions, and caches. In the default deployment it is ephemeral.

```bash
docker compose -f ops/docker/docker-compose.yml exec redis redis-cli ping
docker compose -f ops/docker/docker-compose.yml logs --tail=100 redis worker scheduler
```

If Redis restarts, caches and TTL/session state may disappear; PostgreSQL records should remain. Verify scheduler-to-broker and worker-to-broker connectivity separately. Do not flush a shared Redis instance without confirming every namespace/user.

## Qdrant Issues

Qdrant is a derived index. Check service health, embedding-space/dimension configuration, collection namespace, and PostgreSQL/Qdrant drift:

```bash
curl -fsS http://127.0.0.1:6333/readyz
python -m app.cli.reconcile_vector_index
```

Run a bounded dry repair first when the scope is uncertain:

```bash
python -m app.cli.reconcile_vector_index --repair --dry-run --limit=100
```

Then run the reviewed repair without `--dry-run`. See [Vector Index Synchronization](../vector-index-sync.md).

## Sync Conflicts

Sync ownership is split between `app/api/routers/sync.py`, `app/api/services/sync/`, and `app/infrastructure/persistence/sync_aux_read_adapter.py`. Confirm the session still exists, belongs to the authenticated user/client, and has not expired. Compare the client's cursor/version/idempotency data with the per-item apply result and generated OpenAPI.

Do not resolve a version conflict by blindly overwriting server state. Follow [Sync Protocol](sync-protocol.md) and reproduce the conflicting item against a disposable fixture.

## Refresh Token Stops Working

Inspect the auth response code, refresh-token family row, expiry/revocation state, client/user allowlists, cookie scope, and rate limit. Token rotation invalidates replayed predecessors; a revoked/expired family requires a new login.

Backend ownership:

- `app/api/routers/auth/tokens.py`;
- `app/api/routers/auth/cookies.py`;
- `app/infrastructure/persistence/repositories/auth_repository.py`;
- `app/db/models/core.py::RefreshToken`.

Never log or return the refresh token. See [Mobile API](mobile-api.md#authentication) and [API Errors](api-error-codes.md).

## Mobile API Issues

Use `/health/ready`, the generated OpenAPI document, the HTTP status, error code, and correlation metadata. Check that the client uses the correct `/v1` route, authentication mode, `client_id`, content type, and current request/response schema.

For SSE, verify the request-specific stream route under `app/api/routers/content/streams.py`, proxy buffering/timeouts, reconnect behavior, and terminal event handling.

## MCP Server Issues

Confirm the selected transport/profile, authentication/exposure policy, PostgreSQL/Qdrant connectivity, and current tool/resource enumeration. The maintained surface is in [MCP Server](mcp-server.md). Test read-only mode before enabling write tools or public exposure.

## GitHub Integration Issues

Check token decryption key/rotation state, PAT or Device Flow status, Redis for pending Device Flow, GitHub API response/rate limit, sync budget, `pending_analysis`, and repository/vector persistence. Tokens must remain redacted.

Use [GitHub Sync Runbook](../runbooks/github-sync.md) and [GitHub Repository Ingestion](../explanation/github-repository-ingestion.md).

## YouTube Issues

Separate URL detection, transcript API, yt-dlp download, subtitle parsing, storage limits, ffmpeg merging, and optional transcription fallback. Inspect `video_downloads`, request/crawl records, and correlation-linked logs. Verify the container has ffmpeg and writable configured storage.

See [Configure YouTube](../guides/configure-youtube-download.md).

## Performance Issues

Measure graph-node/provider/LLM timings, queue capacity, database pools/queries, memory, and vector lag. A faster failure is not a successful optimization: compare output quality and terminal status after tuning.

See [Optimize Performance](../guides/optimize-performance.md) and [Observability Strategy](../explanation/observability-strategy.md).

## Getting help

Provide:

- Error ID/correlation ID;
- exact failing operation and expected result;
- sanitized service logs around that ID;
- relevant request/job/crawl/LLM statuses;
- version/commit and deployment topology;
- checks already run and their observed output.

Last audited: 2026-07-15.
