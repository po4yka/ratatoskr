# Set up Redis

Redis is the Taskiq broker and also supports distributed locks, rate limits, sync/OAuth session state, and caches. The maintained Docker Compose topology includes Redis; a full bot/API/worker/scheduler deployment should treat it as a runtime dependency even though individual code paths may degrade without optional cache features.

## Docker Compose

Start Redis with the data services:

```bash
docker compose -f ops/docker/docker-compose.yml up -d redis
docker compose -f ops/docker/docker-compose.yml exec redis redis-cli ping
```

Expected response: `PONG`.

Compose supplies service-local host/port settings to bot/API/worker/scheduler as needed. Do not expose port 6379 publicly.

## External Redis

For an externally managed instance, configure the Redis URL/host settings documented in [Environment Variables](../reference/environment-variables.md). Use TLS/authentication when crossing a trusted network boundary, restrict ACLs to the required keyspace/commands, and test every process role separately.

## Persistence model

Default Compose disables RDB and AOF persistence. Redis state may disappear on restart:

- cached values can be recomputed;
- rate-limit counters reset;
- sync/OAuth sessions may need to restart;
- distributed locks expire/disappear;
- queued Taskiq messages that were not durably consumed can be lost.

Authoritative user and workflow data remains in PostgreSQL, including terminal Taskiq failures. If your recovery requirements need durable broker messages, operate a persistent Redis topology and test its failure semantics explicitly.

## Capacity and keys

Set memory and eviction policy for the combined broker/lock/session/cache workload. An eviction policy that removes live locks or Taskiq messages can cause duplicate work or loss. Monitor memory, connected clients, command latency, evictions, rejected connections, and worker/scheduler broker errors.

Do not run `FLUSHALL` against a shared instance. Inspect namespaces and ownership before deleting keys.

## Verify the application

```bash
docker compose -f ops/docker/docker-compose.yml logs --tail=100 redis worker scheduler
docker compose -f ops/docker/docker-compose.yml exec redis redis-cli INFO memory
```

Create a disposable job or use a normal summary, then confirm the scheduler/worker logs show broker connectivity and PostgreSQL reaches a terminal state.

See [Troubleshooting — Redis](../reference/troubleshooting.md#redis-issues) and [Environment Variables](../reference/environment-variables.md).

Last audited: 2026-07-15.
