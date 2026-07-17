# Status page and system metrics

Ratatoskr exposes a public, sanitized status surface for users and a separate
operator monitoring stack. These surfaces deliberately answer different
questions:

| Surface | Audience | Purpose |
| --- | --- | --- |
| `/status` | Public | Responsive web status page; no authentication or auth boot required. |
| `GET /v1/status` | Public clients | Current bounded component status in the standard success envelope. |
| `GET /health/ready` and `/health/live` | Orchestrator | Minimal readiness and liveness probes. |
| `GET /health/detailed` and `/v1/admin/diagnostics` | Owner | Detailed diagnostics, persisted failures, queues, storage, and provider context. |
| `/internal/metrics` and process-local exporter ports | Prometheus | Internal time-series collection; never a public status API. |
| `Ratatoskr System Status` in Grafana | Operator | Historical rates, latency, saturation, queues, providers, backups, and alerts. |

The public endpoint returns HTTP 200 even when a component is degraded or in
outage. Clients must read `data.status`; keeping the endpoint reachable lets the
status page describe an incident instead of replacing it with a generic HTTP
error.

## Public contract

`GET /v1/status` returns:

- `status`: `operational`, `degraded`, `outage`, `unknown`, or `disabled`;
- a fixed public `message`, generation timestamp, and bounded refresh interval;
- exact component counts in `summary`;
- stable groups and components with status, check time, and optional latency.

The current component map is:

| Component | Signal |
| --- | --- |
| API | The status request is being served. |
| Web application | The reviewed SPA entrypoint is present in the API image. |
| Telegram bot | Its internal Prometheus exporter responds. |
| PostgreSQL | The existing bounded database health check succeeds. |
| Redis | The existing Redis health check succeeds or is explicitly disabled. |
| Qdrant / vector search | The configured vector store reports availability. |
| Scraper / extraction | The bot's local runtime telemetry reports the latest non-policy-blocked chain result; signals older than 24 hours are `unknown`. No synthetic crawl is performed. |
| AI summarization | The worker's allowlisted OpenRouter circuit-breaker updates; only observations from the last 24 hours participate. One failed model degrades the fallback chain, while all fresh observed models open is an outage. Other providers remain `unknown` until they expose an equivalent live signal. |
| Taskiq worker | Its aggregated internal Prometheus exporter responds. |
| Scheduler | Its internal Prometheus exporter responds. |
| Vector reconciliation | Fixed-cardinality worker run and oldest-lag metrics show whether the durable vector repair loop is current. |
| PostgreSQL backup | The node-exporter textfile timestamp is current (up to 36 hours), stale (36–48 hours), or overdue. |
| GitHub repository backups | Persisted GitHub mirror states and last-success timestamps provide coarse complete, partial, or unavailable coverage. |
| ChatGPT backup authorization | Persisted backup lifecycle reports active, unverified, unavailable, or authorization-required state. |
| Claude backup authorization | Persisted backup lifecycle reports active, unverified, unavailable, or authorization-required state. |

Checks run concurrently under a total five-second ceiling and are cached for a
short, validated TTL. Once that ceiling expires, unfinished probes are detached
after cancellation is requested so cancellation-resistant dependencies cannot
hold the public response open. Missing signals are `unknown`, not optimistically
green.
Disabled optional components do not lower the aggregate. A critical component
outage produces overall `outage`; any other outage, degradation, or unknown
signal produces `degraded`.
Prometheus raises a warning when any component remains `unknown` for 15 minutes,
so missing or stale telemetry cannot stay silent indefinitely.

The API lifespan runs the same bounded evaluator immediately and then at half
the shorter cache/client-refresh interval. This keeps status gauges and alerts
current even when nobody has the public page open; the Prometheus scrape route
itself remains a lightweight metrics read.

The response never contains probe URLs, hostnames, repository names or URLs,
account identifiers, backup paths, cookie/session data, exception text,
database details, credentials, provider keys, exact content counts, or
Prometheus samples. Backup and authorization messages are fixed coarse values;
detailed errors remain in owner-only diagnostics and structured logs.

## Metrics topology

Prometheus scrapes the API, bot, Taskiq worker, and scheduler independently.
The worker and multi-worker API use container-local multiprocess registries so
counters and live gauges are aggregated across child processes. The monitoring
profile also supplies pinned PostgreSQL and Redis exporters, scrapes Qdrant
directly, and collects host and backup metrics through node-exporter.

Application instrumentation includes:

- FastAPI HTTP RED metrics with bounded route templates, method, and status class;
- generic Taskiq execution RED metrics with an allowlisted task name;
- public status check count, duration, and current-state gauges;
- existing request, extraction, LLM, vector, social, digest, GitHub, queue,
  cache, authentication, streaming, speech, and circuit-breaker metrics.

Metric labels never contain raw paths, URLs, user IDs, prompts, task IDs, or
exception messages. See [`ops/monitoring/README.md`](../../ops/monitoring/README.md)
for the complete coverage map, dashboard, alert rules, rollout checks, and
rollback procedure.

## Deployment configuration

The primary Compose file injects internal-only exporter locations into
`mobile-api`:

```text
STATUS_BOT_METRICS_URL=http://ratatoskr:9101/metrics
STATUS_WORKER_METRICS_URL=http://worker:9102/metrics
STATUS_SCHEDULER_METRICS_URL=http://scheduler:9103/metrics
STATUS_NODE_METRICS_URL=http://node-exporter:9100/metrics
STATUS_QDRANT_READY_URL=http://qdrant:6333/readyz
```

`DeploymentConfig` validates these as credential-free HTTP URLs and bounds the
per-probe timeout, total timeout, cache TTL, and client refresh interval. The
Qdrant component status comes from a live, bounded `/readyz` request; no response
body or endpoint detail is exposed publicly. Exporter ports use Compose `expose`,
not host `ports`.

Start the complete operator stack with:

```bash
POSTGRES_PASSWORD=... GRAFANA_ADMIN_PASSWORD=... \
docker compose -f ops/docker/docker-compose.yml \
  --profile with-monitoring up -d
```

After rollout, verify `/health/ready`, `/v1/status`, every Prometheus target,
`pg_up`, `redis_up`, and the provisioned Grafana dashboard. A same-host
Prometheus deployment cannot detect loss of the host, Docker daemon, reverse
proxy, DNS, or upstream network. Use an external synthetic monitor for public
availability and paging on those failures.
