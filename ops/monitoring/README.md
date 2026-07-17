# Ratatoskr system-status monitoring

The provisioned `Ratatoskr System Status` dashboard (`ratatoskr-system-status`)
is the single operational view for the application, dependencies, provider
workflows, queues, backups, and Raspberry Pi host. It refreshes every 30
seconds and only queries metric families emitted by the configured scrape
targets.

## Deployment surfaces

The production path is the `with-monitoring` profile in the primary Compose
file:

```bash
export RATATOSKR_ENV=production
export ALERT_WEBHOOK_URL='<receiver-from-secret-store>'
POSTGRES_PASSWORD=... GRAFANA_ADMIN_PASSWORD=... \
docker compose -f ops/docker/docker-compose.yml \
  --profile with-monitoring up -d
```

Instead of `ALERT_WEBHOOK_URL`, or in addition to it, the renderer supports
`ALERT_SLACK_API_URL`, `ALERT_TELEGRAM_WEBHOOK_URL`, and
`ALERT_PAGERDUTY_ROUTING_KEY`. Slack and Telegram URLs must use HTTPS. Every
configured integration is active; receiver credentials are rendered into a
mode-`0600` container-private file and are never printed. A production
Alertmanager without a valid receiver exits before becoming healthy, so
Prometheus and Loki cannot silently depend on a discard endpoint. Inject these
values from the host's secret environment and never commit or log them.

`ops/docker/docker-compose.monitoring.yml` remains available for operators who
run monitoring as a separate Compose project. It joins the application network
named by `RATATOSKR_DOCKER_NETWORK` (default: `docker_default`), which must
already exist. The primary profile is preferred because Compose then owns the
full dependency graph. Run the standalone file instead of, not alongside, the
primary `with-monitoring` profile because they intentionally use the same host
ports and container names.

The default core project name comes from the `ops/docker` directory, so its
network is `docker_default`. If the core stack was started with `-p NAME` or
`COMPOSE_PROJECT_NAME=NAME`, start the separate monitoring stack with
`RATATOSKR_DOCKER_NETWORK=NAME_default` and
`RATATOSKR_PG_BACKUP_METRICS_VOLUME=NAME_pg_backup_metrics`. Verify the network
and its core members before rollout:

```bash
docker network inspect "${RATATOSKR_DOCKER_NETWORK:-docker_default}" \
  --format '{{range .Containers}}{{println .Name}}{{end}}'
docker volume inspect \
  "${RATATOSKR_PG_BACKUP_METRICS_VOLUME:-docker_pg_backup_metrics}" >/dev/null
```

The output must include the API, bot, worker, scheduler, PostgreSQL, Redis, and
Qdrant containers. Prometheus and both dependency exporters attach to that
external network as well as their private monitoring network; Compose fails
instead of silently creating the external network when the name is wrong. The
external backup textfile volume lets standalone node-exporter preserve the same
backup-age panel and alert as the primary monitoring profile.

The Pi overlay runs Qdrant as a native systemd service. It maps the Prometheus
target name `qdrant` to Docker's host gateway, while non-Pi deployments scrape
the Qdrant container with the same `qdrant:6333/metrics` target.

## Coverage map

| Capability | Prometheus source | Dashboard evidence |
|---|---|---|
| API | `up{job="ratatoskr-mobile-api"}`, `ratatoskr_http_requests_total`, `ratatoskr_http_request_duration_seconds`, `ratatoskr_http_requests_in_flight` | Process availability and HTTP RED signals by bounded route template |
| Telegram bot | `up{job="ratatoskr-bot"}` plus `ratatoskr_requests_total` | Process availability and domain request outcomes |
| Taskiq worker | `up{job="ratatoskr-worker"}` plus `ratatoskr_taskiq_executions_total`, `ratatoskr_taskiq_execution_duration_seconds`, `ratatoskr_taskiq_in_flight` | Process availability and generic task RED signals |
| Scheduler | `up{job="ratatoskr-scheduler"}`; scheduled executions are observed on the worker | Scheduler process availability plus task outcomes |
| Public status evaluator | `ratatoskr_status_checks_total`, `ratatoskr_status_check_duration_seconds`, `ratatoskr_status_component_state` | Current bounded component state and check outcomes/latency |
| PostgreSQL | pinned `postgres-exporter`, `pg_up`, `pg_stat_database_*`, `pg_settings_max_connections` | Reachability, connection saturation, buffer-cache ratio |
| Redis | pinned `redis_exporter`, `redis_up`, `redis_memory_*`, `redis_keyspace_*` | Reachability, memory, keyspace/cache ratios, evictions |
| Qdrant/vector index | direct Qdrant `/metrics` scrape availability plus `ratatoskr_vector_*` | Qdrant availability, reconciliation lag and outcomes |
| Scraper chain/Firecrawl | `ratatoskr_scraper_*`, `ratatoskr_firecrawl_*` | Provider success ratio and latency; Firecrawl alerts remain provider-specific |
| LLM/OpenRouter | `ratatoskr_llm_*`, `ratatoskr_openrouter_*`, circuit-breaker gauges | Attempts, latency, breaker state, retry exhaustion, and cost alerts |
| Durable URL work/Taskiq | `ratatoskr_url_processing_queue_depth`, `ratatoskr_url_processor_in_flight`, `ratatoskr_taskiq_retries_total` | Backlog, in-flight work, retry/dead-letter outcomes |
| Digest | `ratatoskr_digest_*` | Delivery outcomes and existing delivery/reconnect alerts |
| GitHub/social | `ratatoskr_github_*`, `ratatoskr_social_*` | Sync outcomes, pending analysis, provider fetch outcomes |
| Backup | node-exporter `ratatoskr_pg_backup_last_success_timestamp_seconds` plus application `ratatoskr_backup_runs_total` and `ratatoskr_backup_items` | PostgreSQL freshness and truthful GitHub/ChatGPT/Claude run outcomes without user or repository labels |
| Host | node-exporter `node_*` metrics | CPU, memory, filesystem saturation and alerts |

Optional integrations are event-driven: if they are disabled or have never
run, their panels show `No data`. That is intentionally distinct from a failed
scrape or a dependency readiness value of zero. The dashboard does not call
LLMs, scrapers, Telegram, GitHub, social providers, Qdrant reconciliation, or
database integrity checks on page refresh.

The API lifespan refreshes the bounded public status evaluator independently of
page views and Prometheus scrapes. Consequently the status component gauges and
alerts remain current without making `/internal/metrics` perform dependency
checks.

The API status aggregator receives the process-local exporter locations through
these internal-only environment variables:

```text
STATUS_BOT_METRICS_URL=http://ratatoskr:9101/metrics
STATUS_WORKER_METRICS_URL=http://worker:9102/metrics
STATUS_SCHEDULER_METRICS_URL=http://scheduler:9103/metrics
STATUS_NODE_METRICS_URL=http://node-exporter:9100/metrics
```

The exporter ports and dependency ports have no host bindings. PostgreSQL and
Redis credentials stay on their exporter containers; Prometheus scrapes only
the exporters' internal HTTP endpoints.

Production starts the API through `python -m app.cli.api_server` with
`PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus-api`. The launcher validates worker
settings and clears stale Prometheus shard files once in the parent process
before importing the app or forking Uvicorn workers. Direct `uvicorn` remains a
single-worker local-development command and must not replace this launcher in
the production Dockerfile or Compose files.

## Rollout verification

Render both Compose variants before changing running containers:

```bash
POSTGRES_PASSWORD=contract GRAFANA_ADMIN_PASSWORD=contract \
docker compose -f ops/docker/docker-compose.yml \
  --profile with-monitoring config >/dev/null

POSTGRES_PASSWORD=contract GRAFANA_ADMIN_PASSWORD=contract \
docker compose -f ops/docker/docker-compose.monitoring.yml config >/dev/null
```

After rollout, verify target and dependency readiness from Prometheus:

```bash
curl -fsS 'http://127.0.0.1:9090/api/v1/query?query=up'
curl -fsS 'http://127.0.0.1:9090/api/v1/query?query=pg_up'
curl -fsS 'http://127.0.0.1:9090/api/v1/query?query=redis_up'
```

Then post a synthetic alert and confirm it reaches every configured receiver:

```bash
curl -fsS -H 'Content-Type: application/json' \
  -d '[{"labels":{"alertname":"RatatoskrReceiverSmokeTest","severity":"warning"},"annotations":{"summary":"Alert delivery smoke test"}}]' \
  http://127.0.0.1:9093/api/v2/alerts
```

Do not consider the monitoring rollout complete until the external delivery is
observed. The smoke-test alert resolves automatically after Alertmanager's
configured timeout.

Expected current values are `1` for the four application jobs, `postgres`,
`redis`, `qdrant`, and `node`, and for both `pg_up` and `redis_up`. Then open
Grafana on `http://127.0.0.1:3001` and check that `Ratatoskr System Status`
loads without query errors. A panel can legitimately have no data only for an
optional or not-yet-exercised workflow.

This monitoring stack runs on the same host as the application, so it cannot
prove whole-host or whole-network availability. Use an external synthetic
monitor for public availability and paging on a Pi/Docker daemon outage.

## Rollback

Monitoring changes do not require an application data rollback. To roll back:

1. restore the previous `prometheus.yml`, `alerting_rules.yml`, dashboard, and
   Compose definitions;
2. recreate Prometheus and Grafana so they reload the restored files;
3. remove the two exporter containers after their scrape jobs are gone;
4. verify the four application scrape targets still report `up == 1`.

Example after restoring the files:

```bash
POSTGRES_PASSWORD=... GRAFANA_ADMIN_PASSWORD=... \
docker compose -f ops/docker/docker-compose.yml \
  --profile with-monitoring up -d --remove-orphans prometheus grafana
```

Removing exporters only removes dependency telemetry. It does not modify
PostgreSQL, Redis, Qdrant, their volumes, or application state.
