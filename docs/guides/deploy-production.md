# Ratatoskr — Setup & Deployment Guide

This guide explains how to prepare environments, configure secrets, and run the service locally and in production (Docker).

## Prerequisites

- Python 3.13+
- Telegram account and bot token
- OpenRouter API key
- Self-hosted scraper sidecars are optional but recommended (Firecrawl, Crawl4AI, Defuddle); started via `with-scrapers` compose profile — no cloud API keys required
- Docker (for containerized deployment)
- Node.js 20+ (optional; needed for local frontend development in the **ratatoskr-web** repo)
- (Optional) Redis for API rate limits/sync locks

## Telegram Setup

1. Create a Telegram app to obtain `API_ID` and `API_HASH`: - https://my.telegram.org/apps
2. Create a bot via BotFather to obtain `BOT_TOKEN`: - https://t.me/BotFather → `/newbot`
3. Restrict access to your Telegram user ID(s): - Find your numeric user ID with any Telegram “user id” bot, then set `ALLOWED_USER_IDS` to a comma‑separated list, e.g. `ALLOWED_USER_IDS=123456789`.
4. The bot registers command hints (`/help`, `/summarize`) automatically on startup for private chats.

## OpenRouter Setup

- Sign up: https://openrouter.ai/
- Create an API key and set `OPENROUTER_API_KEY`.
- Choose a model (e.g., `deepseek/deepseek-v4-flash`) and set `OPENROUTER_MODEL`.
- Optional attribution: `OPENROUTER_HTTP_REFERER`, `OPENROUTER_X_TITLE`.

## Scraper Sidecar Setup (Optional)

Cloud Firecrawl is not used for article extraction. `FIRECRAWL_API_KEY` is no longer required.

The default scraper chain order is: `scrapling -> crawl4ai -> firecrawl_self_hosted -> defuddle -> playwright -> crawlee -> direct_html -> scrapegraph_ai`. Order is overridable via `SCRAPER_PROVIDER_ORDER`.

- **Scrapling**: enabled by default, no external dependency.
- **Crawl4AI**: HTTP sidecar at port 11235. Enable with `SCRAPER_CRAWL4AI_ENABLED=true` and start via the `with-scrapers` profile.
- **Self-hosted Firecrawl**: set `FIRECRAWL_SELF_HOSTED_ENABLED=true` and start via `with-scrapers`. Internal endpoint: `http://firecrawl-api:3002`.
- **Defuddle**: self-hosted Node sidecar at `defuddle-api:3003`. Started automatically by `with-scrapers`. Default is now enabled (`SCRAPER_DEFUDDLE_ENABLED=true`, `SCRAPER_DEFUDDLE_API_BASE_URL=http://defuddle-api:3003`).
- **ScrapeGraphAI**: in-process last-resort LLM provider, no sidecar required. Enable with `SCRAPER_SCRAPEGRAPH_ENABLED=true`.
- Breaking rename note: legacy vars (`SCRAPLING_*`, `SCRAPER_DIRECT_HTTP_ENABLED`) now fail fast at startup.

See `docs/reference/environment-variables.md` for the full multi-provider scraper chain configuration.

## Environment Variables (essentials)

Copy `.env.example` to `.env` and fill only the first-run required values:

- Telegram: `API_ID`, `API_HASH`, `BOT_TOKEN`, `ALLOWED_USER_IDS`
- OpenRouter: `OPENROUTER_API_KEY`

Optional runtime, scraper, YouTube, Twitter/X, MCP, and model tuning belongs in `ratatoskr.yaml`; see `docs/reference/config-file.md`. `JWT_SECRET_KEY` is required only when web/API/browser-extension JWT auth is enabled.

## Local Development

1) Create venv & install: `make venv && source .venv/bin/activate && pip install -r requirements.txt -r requirements-dev.txt`
2) Export env or use `.env`.
3) Tests: `make test` (or `make lint`, `make format`, `make type` as needed).
4) Run Telegram bot: `python bot.py`
5) Run API host (serves `/v1/*`, `/static/*`, and `/web/*`): `uvicorn app.api.main:app --reload --host 0.0.0.0 --port 8000`
6) Optional web frontend local loop (run from the **ratatoskr-web** repo): - `npm ci && npm run dev` (Vite dev server) - `npm run check:static` (lint + typecheck)

## Web Interface Serving Contract

The FastAPI service serves the web frontend from a static bundle:

- Web static files: `/static/web/*`
- Web SPA entry routes: `/web` and `/web/{path:path}` (returns `app/static/web/index.html`)

If `/web` returns `404 "Web interface is not built"`, build and deploy from the **ratatoskr-web** repo:

```bash
# In ratatoskr-web repo
npm ci && npm run build
cp -r dist/ /path/to/ratatoskr/app/static/web/
```

How to use (no commands needed)

- You can simply send a URL (or several URLs in one message) or forward a channel post — the bot will summarize it.
- You can also use `/aggregate` to synthesize one mixed-source bundle from one or more links and, when applicable, the current forwarded/attached Telegram content.
- Commands are optional helpers: - `/summarize <URL>` or `/summarize` then send URL - `/summarize_all <URLs>` to process many URLs immediately - `/aggregate <URLs>` to request one bundle-level aggregation - `/summarize_forward` then forward a channel post

## Docker Deployment

1) Lock deps: `make lock-uv`.
2) Build: `docker build -f ops/docker/Dockerfile -t ratatoskr .`
3) Run:

```
docker run --env-file .env \
  -v $(pwd)/data:/data \
  -p 8000:8000 \  # expose API if needed
  --name ratatoskr --restart unless-stopped ratatoskr
```

Notes

- SQLite at `/data/ratatoskr.db`; backups under `/data/backups`. Mount `/data` for durability.
- Set `ALLOWED_USER_IDS`; keep `DEBUG_PAYLOADS=0` in prod.
- If using web/API/browser-extension JWT auth, ensure `JWT_SECRET_KEY` is set and port 8000 exposed.
- Web assets are built in **ratatoskr-web** and deployed into `app/static/web/` via CI/CD; they are served by FastAPI under `/static/web/*`.

## Docker Compose (recommended)

### Production vs development compose

| Use case | Command |
|---|---|
| **Production** (immutable image) | `docker compose -f ops/docker/docker-compose.yml up -d` |
| **Local dev** (live source reload) | `docker compose -f ops/docker/docker-compose.yml -f ops/docker/docker-compose.dev.yml up -d` |
| **Pi / remote deploy** | `make pi-deploy` (builds locally, streams image, restarts via Pi overlay) |

The production compose bakes all app code (`app/`, `bot.py`, `alembic.ini`) into the image at build time and mounts only `/data` (persistent storage) and `/etc/localtime` (timezone). No source tree directories are bind-mounted.

The `docker-compose.dev.yml` overlay re-adds source bind mounts (`app/`, `bot.py`, `alembic.ini`, `config/`) for edit-and-restart iteration without a full rebuild. **Never use this overlay on the Pi** — it shadows the shipped image and defeats `make pi-deploy`.

Optional YAML config (`config/ratatoskr.yaml`, `config/models.yaml`) is not mounted in production. Use environment variables in `.env` instead (all YAML keys map 1-to-1 to env vars). If you prefer the file-based approach, set `RATATOSKR_CONFIG=/data/ratatoskr.yaml` and `MODELS_CONFIG_PATH=/data/models.yaml` and place the files in your `data/` directory — that volume is already mounted.

The production `ops/docker/docker-compose.yml` defines a 7-service stack:

```yaml
services:
  migrate:              # One-shot migration job — exits 0 before app services start
    command: ["python", "-m", "app.cli.migrate_db"]
    restart: "no"
    depends_on: [postgres]

  ratatoskr:            # Telegram bot
    command: ["python", "-m", "bot"]
    depends_on: [migrate, postgres, redis, qdrant (optional)]
    healthcheck: DB ping (asyncpg) every 30s

  worker:               # Taskiq task executor (consumes jobs from Redis)
    command: ["taskiq", "worker", "app.tasks.broker:broker", ...]
    depends_on: [migrate, postgres, redis, qdrant (optional)]
    healthcheck: Redis TCP :6379 every 30s

  scheduler:            # Taskiq task enqueuer — singleton, emits cron jobs
    command: ["taskiq", "scheduler", "app.tasks.scheduler:scheduler", "--skip-first-run"]
    depends_on: [migrate, redis]
    healthcheck: Redis TCP :6379 every 30s

  mobile-api:           # FastAPI REST API
    build: {context: ../.., dockerfile: ops/docker/Dockerfile.api}
    ports: ["127.0.0.1:18000:8000"]
    depends_on: [migrate, redis, qdrant (optional)]
    healthcheck: DB ping (asyncpg) every 30s

  redis:                # Taskiq broker, rate limits, distributed locks
    image: redis:7-alpine

  qdrant:               # Vector search
    image: qdrant/qdrant:v1.13.6
```

Run the core stack: `docker compose -f ops/docker/docker-compose.yml up -d --build`

Run core plus self-hosted scraper sidecars (Firecrawl, Crawl4AI, Defuddle):

```bash
FIRECRAWL_SELF_HOSTED_ENABLED=true \
docker compose -f ops/docker/docker-compose.yml --profile with-scrapers up -d --build
```

Run with a remote Ollama-compatible provider:

```bash
LLM_PROVIDER=ollama \
OLLAMA_BASE_URL=https://ollama.example.com/v1 \
OLLAMA_API_KEY=replace_with_provider_token \
OLLAMA_MODEL=llama3.3 \
docker compose -f ops/docker/docker-compose.yml --profile with-cloud-ollama up -d --build
```

Run with monitoring:

```bash
docker compose -f ops/docker/docker-compose.yml --profile with-monitoring up -d --build
```

## Optional Subsystems

These profile services are not required but enhance functionality when available:

- **Self-hosted scraper sidecars** (`with-scrapers`) -- starts `firecrawl-api` (port 3002) plus its Playwright, Redis, RabbitMQ, and Postgres dependencies, `crawl4ai` (port 11235), and `defuddle-api` (port 3003). Cloud Firecrawl is not a deployment option for the article extraction path. Image defaults follow upstream `latest`; pin `FIRECRAWL_IMAGE`, `FIRECRAWL_PLAYWRIGHT_IMAGE`, and `FIRECRAWL_POSTGRES_IMAGE` in production when you need repeatable rebuilds.
- **Cloud Ollama** (`with-cloud-ollama`) -- does not start a local model server. It configures Ratatoskr for a remote OpenAI-compatible `/v1` endpoint and runs a lightweight `/models` reachability check. Structured JSON quality depends on the remote model; OpenRouter remains the primary quality path.
- **Monitoring** (`with-monitoring`) -- Prometheus, Grafana, Loki, Promtail, and node-exporter from the primary compose file.
- **MCP Server** (`mcp`, `mcp-write`, `mcp-public`) -- Exposes article, search, Qdrant, and aggregation tools/resources to external AI agents (OpenClaw, Claude Desktop, hosted SSE clients). Runs as a dedicated Docker container with SSE transport (`ratatoskr-mcp`) or standalone via `python -m app.cli.mcp_server`. See `docs/reference/mcp-server.md`.
- **Channel Digest** -- Scheduled digests of subscribed Telegram channels. Set `DIGEST_ENABLED=true` and `API_BASE_URL` to the Mobile API endpoint. Run `/init_session` in the bot to authenticate the userbot via Mini App OTP/2FA flow, then use `/subscribe @channel` to add channels.
- **Connected social accounts** -- OAuth-backed X, Instagram, and Threads connections are managed from Telegram with `/social`, `/connect_x`, `/connect_threads`, `/connect_instagram`, and `/disconnect_social <provider>`. Configure the provider client IDs, secrets, redirect URIs, and scopes from `docs/reference/environment-variables.md`. Monitor `ratatoskr_social_fetch_total`, `ratatoskr_social_token_refresh_total`, `ratatoskr_social_rate_limit_total`, and `ratatoskr_social_connection_status_total` when enabling authenticated social fetching; fetch-attempt metadata is sanitized before persistence and logs keep correlation IDs without tokens.

Full variable reference: `docs/reference/environment-variables.md`

## GitHub Integration Secrets

These variables are only required when users connect GitHub accounts.

### GITHUB_TOKEN_ENCRYPTION_KEY (REQUIRED if any user connects GitHub)

Fernet symmetric key used to encrypt stored GitHub tokens at rest.

Generate once:

```bash
python tools/scripts/generate_github_encryption_key.py
```

Add the output to `.env`:

```env
GITHUB_TOKEN_ENCRYPTION_KEY=<base64-fernet-key>
```

**This key must never change without a rotation procedure.** A lost or replaced key renders all stored tokens unreadable and all GitHub integrations broken. To rotate, follow the MultiFernet hint in `app/security/token_crypto.py` (use `MultiFernet([new_key, old_key])` to decrypt old tokens while encrypting new ones, then swap to `[new_key]` after all rows are re-encrypted).

### GITHUB_OAUTH_APP_CLIENT_ID and GITHUB_OAUTH_APP_CLIENT_SECRET (OPTIONAL)

Only required for the OAuth Device Flow (`POST /v1/auth/github/device/start`). The PAT path (`POST /v1/auth/github/pat`) works without these.

Register an OAuth App at https://github.com/settings/applications/new. The callback URL is unused for Device Flow (set it to your domain for record-keeping).

```env
GITHUB_OAUTH_APP_CLIENT_ID=Iv1.abc123...
GITHUB_OAUTH_APP_CLIENT_SECRET=<secret>
```

### Redis dependency

Device Flow requires Redis to store pending device codes. PAT auth has no Redis dependency. If `REDIS_URL` is not set, `POST /v1/auth/github/device/start` returns `503`.

### LLM cost for first sync

The first sync for a user with many starred repositories can be expensive. With the default daily budget of 50 repos at ~$0.002 per analysis, a 1 000-star user amortizes across approximately 20 days. Tune `GITHUB_SYNC_LLM_DAILY_BUDGET` and `GITHUB_SYNC_LLM_CONCURRENCY` before onboarding heavy users.

---

## Security & Hardening

- Access control: set `ALLOWED_USER_IDS`; restrict `ALLOWED_CLIENT_IDS` for API if used.
- Resource control: configure rate limits (`API_RATE_LIMIT_*`) and concurrency caps; prefer Redis.
- Aggregation guardrails: tune `API_RATE_LIMIT_AGGREGATION_CREATE_USER` and `API_RATE_LIMIT_AGGREGATION_CREATE_CLIENT` before exposing `/v1/aggregations` to external clients.
- Aggregation URL safety: `/v1/aggregations` rejects localhost, private-network, and other reserved-address targets before extraction starts.
- Secrets: use `.env` or secret manager; never commit secrets.
- Logs: JSON with correlation IDs; redact `Authorization`.
- Container: least privilege; restrict `/data` permissions on host; HTTPS termination in front of API.

### External CLI and Hosted MCP Rollout Checklist

Before onboarding external users, verify:

1. `SECRET_LOGIN_ENABLED=true` on the API service.
2. `ALLOWED_CLIENT_IDS` is explicitly set for the client IDs you plan to issue, for example `cli-workstation-v1,mcp-agent-v1`.
3. Client IDs follow stable prefixes such as `cli-*`, `mcp-*`, `automation-*`, `web-*`, or `mobile-*`.
4. Operators understand that plaintext client secrets are visible only at create or rotate time.
5. External self-service secret issuance remains limited to `cli-*`, `mcp-*`, and `automation-*` clients unless you intentionally widen the model later.

Before exposing hosted public MCP, also verify:

1. `MCP_TRANSPORT=sse`
2. `MCP_AUTH_MODE=jwt`
3. `MCP_USER_ID` is unset for hosted request-scoped mode
4. `MCP_ALLOW_REMOTE_SSE=true` only when you intentionally bind beyond loopback
5. `MCP_FORWARDING_SECRET` is configured if a trusted gateway forwards bearer tokens
6. the MCP deployment has writable access to the database if you want aggregation write tools enabled
7. `/v1/aggregations` and `/sse` both sit behind HTTPS and normal ingress logging/monitoring

### External Access Stage Order

Roll out external aggregation access in this order and only promote when the previous stage stays within the safety thresholds for at least 24 hours:

1. Internal API users only
2. Invite-only external CLI users
3. Local stdio MCP users with write-capable aggregation tools
4. Trusted hosted SSE MCP beta behind a gateway
5. Broad external enablement

Watch these signals during every stage:

- `ratatoskr_requests_total{type="aggregation.create",status=...,source="cli"}` for CLI create volume and error rate
- `ratatoskr_requests_total{type="aggregation.create",status=...,source="api"}` for direct API callers without a typed client prefix
- `ratatoskr_requests_total{type="<tool>",status=...,source="mcp"}` for MCP tool adoption and failures
- `ratatoskr_request_latency_seconds{type="aggregation.create",stage="total"}` for end-to-end API create latency
- `ratatoskr_request_latency_seconds{type="<tool>",stage="total"}` for MCP tool latency
- `ratatoskr_aggregation_bundles_total{status=...,entrypoint=...}` for completed, partial, and failed bundles
- `ratatoskr_aggregation_extraction_total{platform=...,outcome=...}` for per-platform extraction failure spikes
- `ratatoskr_aggregation_bundle_latency_seconds{entrypoint=...}` for bundle completion latency
- `ratatoskr_aggregation_synthesis_coverage_ratio_bucket{source_type=...,status=...}` for low-coverage summaries

Use these go/no-go thresholds:

- Promote only if `aggregation.create` and MCP write-tool error rates stay below 5% over the last 24 hours.
- Hold the rollout if failed plus partial bundles exceed 10% of total bundles for any stage.
- Hold the rollout if p95 `aggregation.create` latency exceeds 30 seconds for CLI/API traffic or if p95 MCP write-tool latency exceeds 15 seconds.
- Hold the rollout if any single platform in `ratatoskr_aggregation_extraction_total` shows a failure rate above 20%.
- Hold the rollout if low-coverage syntheses (`coverage_ratio < 0.5`) exceed 10% of completed mixed bundles.
- Roll back immediately on confirmed cross-user access, auth bypass, SSRF bypass, or sustained 429 saturation caused by the new client cohort.

Stage-specific promotion checks:

- Stage 1 to Stage 2: keep invite-only CLI clients on an explicit allowlist, verify client IDs map cleanly to `cli-*`, and confirm successful create/get/list flows for at least three distinct external users.
- Stage 2 to Stage 3: verify local MCP aggregation writes use scoped identities only, and confirm `ratatoskr_requests_total{source="mcp"}` shows zero auth or access-denied surprises for trusted testers.
- Stage 3 to Stage 4: verify hosted SSE traffic arrives through JWT or trusted forwarded-token auth only, confirm request-scoped reads and writes in logs, and require no security incidents during the beta window.
- Stage 4 to Stage 5: confirm support docs, troubleshooting coverage, rate-limit capacity, and operator dashboards are all in active use before widening access.

Rollback triggers and actions:

- If any hold or rollback condition is hit, stop issuing new external secrets, pause hosted MCP onboarding, and set `AGGREGATION_ROLLOUT_STAGE` back to the last safe stage.
- If hosted MCP is implicated, disable public exposure first by setting `MCP_ALLOW_REMOTE_SSE=false` or removing the public ingress route.
- If only secret-based external access is implicated, set `SECRET_LOGIN_ENABLED=false` while keeping internal bot/API traffic available.
- Rotate affected client secrets, review `aggregation.bundle_create_*` audit events, and restore the previous known-good image before reopening the stage.

## Operations

- Health: ensure the bot account stays unbanned and tokens valid.
- Monitoring: watch logs for latency spikes and error rates; consider dashboarding via structured logs.
- Aggregation observability: Grafana provisioning includes `ops/monitoring/grafana/provisioning/dashboards/ratatoskr-aggregation.json` for bundle cost, latency, partial-success, and coverage tracking.
- Backups: automatic snapshots land in `/data/backups`. Copy them off-host or adjust `DB_BACKUP_*` if you need a different cadence.

### Scheduler singleton

The `scheduler` service has `deploy.replicas: 1` (Compose default). Never run two scheduler instances against the same Redis broker — they each enqueue every task once per tick, resulting in duplicate job execution. In Docker Swarm, set `deploy.mode: replicated` with `replicas: 1` explicitly. On the Pi (systemd + Docker Compose), the compose stack already guarantees a single instance.

### Health Checks

| Service    | Method                          | Interval | Details                                                              |
| ---------- | ------------------------------- | -------- | -------------------------------------------------------------------- |
| migrate    | (none — one-shot exits 0/non-0) | —        | `restart: no`; dependants use `condition: service_completed_successfully` |
| ratatoskr  | DB ping via asyncpg             | 30s      | Verifies DB connectivity; 5 retries, 60s start period                |
| worker     | Redis TCP :6379                 | 30s      | Verifies broker reachability; 5 retries, 30s start period            |
| scheduler  | Redis TCP :6379                 | 30s      | Verifies broker reachability; 5 retries, 30s start period            |
| mobile-api | DB ping via asyncpg             | 30s      | Verifies DB ready; 5 retries, 60s start period                       |
| redis      | `redis-cli ping`                | 10s      | Standard Redis liveness; 5 retries                                   |
| qdrant     | HTTP `GET /healthz`             | 30s      | Qdrant health endpoint; 3 retries, 60s start period                  |

## Updating a Running Instance

When deploying a new version to a host that already has the service running:

### 1. Backup

```bash
cd /path/to/ratatoskr
tar czf ~/ratatoskr-backup-$(date +%Y%m%d%H%M).tgz data .env
```

The container also writes automatic snapshots to `data/backups/`.

Application backups created from `/v1/backups` now include integrity metadata: `checksumSha256`, `itemCounts`, `schemaVersion`, `createdAt`, `verifiedAt`, `verificationStatus`, and `verificationError`. Before trusting a downloaded archive, check that `verificationStatus` is `verified`; if you copy backup files between hosts, run `POST /v1/backups/{backup_id}/verify` after the file lands to re-check the checksum and archive structure.

Before importing an archive, use `POST /v1/backups/restore/dry-run` with the backup ZIP. The dry run validates the format, checks schema compatibility, reports entity counts and estimated affected rows, and does not mutate the production database. The existing `POST /v1/backups/restore` endpoint performs an import-style restore; there is intentionally no automatic destructive restore workflow.

If you enable scheduled/user backups, set `backup_retention_count` to a positive integer. Retention cleanup only prunes terminal `completed` or `failed` backup records beyond the retention window and leaves `pending` or `processing` backups untouched.

Before running a self-hosted instance long term, choose a raw-data retention posture in `.env`. The defaults keep summaries/search data while aging out scraped article bodies, Telegram raw JSON, video transcripts, downloaded media files, LLM prompt/response payloads, request content text, and orphaned export files after their subsystem TTLs. Set `RETENTION_PRIVACY_NO_RETENTION_MODE=true` for a best-effort low-retention setup: new crawl and LLM write paths skip avoidable raw payload persistence, and the purge task nulls raw fields on its next run while preserving summaries, costs, statuses, and search metadata. The tradeoff is operational: offline re-reading/debugging from stored raw content becomes weaker after cleanup, so keep short nonzero TTLs instead of no-retention mode if you need time to inspect bad scraper output or LLM responses.

### 2. Pull latest

```bash
git fetch --all --prune
git checkout main
git pull --ff-only
```

Pin to a specific tag: `git checkout tags/<tag>`

### 3. Refresh deps (when pyproject/lock changed)

```bash
make lock-uv
```

> **First upgrade onto Ratatoskr from `bite-size-reader`?** Read [Migrate from bite-size-reader](migrate-from-bite-size-reader.md) first — it covers the renamed Docker image, MCP URIs / headers, Prometheus metric names, web storage keys, and the retired Karakeep integration.

### 4. Rebuild & redeploy

```bash
# Compose (recommended)
docker compose -f ops/docker/docker-compose.yml down
docker compose -f ops/docker/docker-compose.yml up -d --build

# Or manual
docker stop ratatoskr && docker rm ratatoskr
docker build -f ops/docker/Dockerfile -t ratatoskr:latest .
docker run -d --env-file .env -v $(pwd)/data:/data \
  -p 8000:8000 --name ratatoskr --restart unless-stopped ratatoskr:latest
```

> The `migrate` service runs automatically before `ratatoskr`, `worker`, `scheduler`, and `mobile-api` start. There is no need to run migrations manually.

### 5. Verify

```bash
docker ps
docker logs -f ratatoskr
```

Send a test message from a whitelisted Telegram account.

### 6. Rollback

Stop the container, restore the backup tarball, and restart the previous image:

```bash
git checkout <previous-commit-sha>
docker compose -f ops/docker/docker-compose.yml up -d --build
```

## Troubleshooting

- "Access denied": verify `ALLOWED_USER_IDS` contains your Telegram numeric ID.
- "Failed to fetch content": scraper chain exhausted all providers; check sidecar health (`curl localhost:3002/health`, `curl localhost:11235/health`, `curl localhost:3003/health`) and review logs for `scraper_chain_exhausted`. See `docs/reference/troubleshooting.md` for diagnosis steps.
- "LLM error": OpenRouter API issue or model outage; rely on built-in retries/fallbacks; check logs.
- Missing deps after update: rebuild the Docker image.
- Secrets/env issues: recheck `.env` (quote special chars).
- Telegram auth: verify `API_ID`, `API_HASH`, `BOT_TOKEN`; ensure bot not banned.
- DB permissions: ensure host `data/` is writable by the Docker user.
- Large summaries: The bot returns JSON in a message; if too large, consider implementing file replies.
- `/web` returns 404: web bundle is missing; build and deploy from the **ratatoskr-web** repo (`npm run build`, then copy `dist/` into `app/static/web/`).
