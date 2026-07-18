# Deploy Ratatoskr to production

This guide deploys the current Docker Compose stack. It assumes a single operator or a deliberately configured allowlisted deployment.

## Prerequisites

- Docker Engine with the Compose plugin;
- Telegram `API_ID`, `API_HASH`, and bot token;
- PostgreSQL password;
- one configured LLM provider: OpenRouter, OpenAI, Anthropic, or Ollama;
- a reverse proxy with TLS when exposing the HTTP API beyond localhost;
- enough storage for PostgreSQL, media, backups, and optional browser sidecars.

For Raspberry Pi deployment, use the project workflow in [Pi deployment](../../CLAUDE.md#docker-image-name-footgun-pi-deploy) instead of building on the Pi.

## Configure secrets

Copy the example and fill the required values:

```bash
cp .env.example .env
```

At minimum, configure:

```env
API_ID=
API_HASH=
BOT_TOKEN=
ALLOWED_USER_IDS=
POSTGRES_PASSWORD=
DATABASE_URL=postgresql+asyncpg://ratatoskr_app:<password>@postgres:5432/ratatoskr
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=
```

For a direct provider, replace the last two lines with the selected provider's key, model, and endpoint settings. See [Configure LLM Provider](configure-llm-provider.md).

Use a long random `JWT_SECRET_KEY` only when enabling HTTP/web authentication. Configure `ALLOWED_CLIENT_IDS` for external clients. Keep secrets in `.env` or a deployment secret manager; the YAML loader ignores secret-marked keys.

## Configure non-secret settings

The maintained YAML file is `config/ratatoskr.yaml`. It is copied into the application images at build time and supplies non-secret model, scraper, media, and runtime settings.

To keep an operator-owned copy under the persistent `/data` mount, set:

```env
RATATOSKR_CONFIG=/data/ratatoskr.yaml
```

and place the file at `data/ratatoskr.yaml` before starting services. The former `models.yaml` and `MODELS_CONFIG_PATH` contract no longer exists.

## Build and migrate

From the repository root:

```bash
docker compose -f ops/docker/docker-compose.yml build
docker compose -f ops/docker/docker-compose.yml up -d postgres redis qdrant
docker compose -f ops/docker/docker-compose.yml run --rm migrate \
  python -m app.cli.migrate_db --apply
docker compose -f ops/docker/docker-compose.yml up -d
```

Application containers also run `python -m app.cli.migrate_db --check` before their main process. They stop when the live schema is not at Alembic head; they do not apply migrations implicitly.

## Base services

The base Compose file defines nine application/data roles:

| Service | Role |
|---|---|
| `migrate` | One-shot Alembic migration command. |
| `ratatoskr` | Telethon bot and synchronous ingress. |
| `worker` | Taskiq job executor. |
| `scheduler` | Singleton Taskiq cron scheduler. |
| `mobile-api` | FastAPI application, bound to `127.0.0.1:18000` by default. |
| `postgres` | PostgreSQL relational source of truth. |
| `pg-backup` | Scheduled PostgreSQL dump sidecar. |
| `redis` | Taskiq broker, locks, rate limits, and ephemeral cache state. |
| `qdrant` | Derived vector index for semantic/hybrid retrieval. |

The bot, API, worker, and scheduler are separate containers built from the project images. Source code and `config/` are baked into those images; the production Compose file intentionally does not bind-mount them. Use `ops/docker/docker-compose.dev.yml` only for local development.

## Optional profiles

| Profile | Adds |
|---|---|
| `with-scrapers` | Self-hosted Firecrawl and dependencies, Crawl4AI, Defuddle, and CloakBrowser. |
| `with-webwright` | Microsoft Webwright browser-agent sidecar. |
| `with-monitoring` | Prometheus, Alertmanager, Grafana, Loki, Promtail, node-exporter, PostgreSQL/Redis exporters, and tracing services. |
| `with-cloud-ollama` | Reachability check for a remote OpenAI-compatible Ollama endpoint. |
| `mcp`, `mcp-write`, `mcp-public` | MCP server variants with distinct exposure/write policies. |

Example:

```bash
FIRECRAWL_SELF_HOSTED_ENABLED=true \
docker compose -f ops/docker/docker-compose.yml \
  --profile with-scrapers up -d --build
```

The default generic chain has 13 positions: Reddit, Hacker News, Scrapling, direct PDF, Crawl4AI, self-hosted Firecrawl, Defuddle, CloakBrowser, Playwright, Crawlee, direct HTML, ScrapeGraphAI, and Webwright. Providers skip when disabled, unsupported, or unavailable. See [Scraper Chain](../explanation/scraper-chain.md).

## HTTP exposure

Keep `mobile-api` bound to loopback and terminate TLS at a reverse proxy. Proxy the application origin, including `/v1`, `/static`, SSE, auth callbacks, and root SPA routes, without stripping correlation or authorization headers. Configure the externally reachable base/callback URLs for enabled OAuth and digest features.

Do not expose PostgreSQL, Redis, Qdrant, scraper sidecars, or Webwright directly to the public network.

## Verify

```bash
docker compose -f ops/docker/docker-compose.yml ps
docker compose -f ops/docker/docker-compose.yml logs --tail=100 ratatoskr worker scheduler mobile-api
curl -fsS http://127.0.0.1:18000/health/ready
```

Then send a URL from an allowed Telegram account and confirm:

- the bot returns a structured summary;
- `requests`, `crawl_results`, `llm_calls`, and `summaries` receive records;
- a user-visible failure includes its Error ID;
- configured vector search returns the new summary or the reconciler reports why it cannot.

## Backups

The `pg-backup` service writes scheduled encrypted PostgreSQL dumps to the
configured host directory. Set `BACKUP_ENCRYPTION_KEY` in the deployment secret
store before starting the stack; the sidecar fails closed when it is missing.
Also protect media/session directories, operator configuration, encryption
keys, and any Qdrant snapshot you need for fast restoration. Redis is ephemeral
in the default stack.

Follow [Back Up and Restore](backup-and-restore.md) and rehearse [Disaster Recovery](../runbooks/disaster-recovery.md) before relying on the deployment.

## Update

```bash
git pull --ff-only
docker compose -f ops/docker/docker-compose.yml build
docker compose -f ops/docker/docker-compose.yml run --rm migrate \
  python -m app.cli.migrate_db --apply
docker compose -f ops/docker/docker-compose.yml up -d
```

Review migrations and [Migrate Versions](migrate-versions.md) before applying them. A database migration can constrain rollback even when an older image is still available.

## Raspberry Pi

The Pi workflow builds Linux/ARM64 images on the development machine, streams them over SSH, applies migrations explicitly, and recreates the target services:

```bash
make pi-migrate
make pi-migrate APPLY=1
make pi-deploy-all
```

`make pi-deploy-all` ships the bot, worker, scheduler, API, and `pg-backup`
images, then recreates all five services. The backup sidecar runs one dump on
startup so the backup directory and node-exporter textfile metric exist before
the deploy reports it healthy. Use `make pi-deploy SERVICE=mobile-api` or
`make pi-deploy SERVICE=pg-backup` for a targeted image and the project rollback
targets for retained previous images. The Pi does not run `docker build`.

## Troubleshooting

Start with the failing service logs and correlation ID, then use [Troubleshooting](../reference/troubleshooting.md). Configuration is indexed in [Environment Variables](../reference/environment-variables.md); secret rotation procedures are in [Secret Rotation](../runbooks/secret-rotation.md).
