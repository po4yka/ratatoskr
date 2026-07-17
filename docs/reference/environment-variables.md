# Environment Variables Reference

Ratatoskr separates secrets from non-secret tuning:

- `.env` or process environment: secrets, credentials, PII, and deployment
  wiring;
- `ratatoskr.yaml`: non-secret application settings;
- `ops/docker/docker-compose.yml`: per-role and sidecar deployment settings.

The executable catalog is the Pydantic configuration under `app/config/`. Every
field declares its environment name with `validation_alias`; some fields accept
additional legacy aliases. This page documents ownership and operationally
important inputs without duplicating hundreds of tunables and their validators.

## First-run values

The checked-in `.env.example` is the canonical minimal template for the default
Telegram + OpenRouter + Compose path:

| Variable | Required when | Owner |
| --- | --- | --- |
| `API_ID` | Running the Telegram bot | `TelegramConfig` |
| `API_HASH` | Running the Telegram bot | `TelegramConfig` |
| `BOT_TOKEN` | Running the Telegram bot | `TelegramConfig` |
| `ALLOWED_USER_IDS` | Owner/allowlist deployment | `TelegramConfig` |
| `POSTGRES_PASSWORD` | Using the bundled PostgreSQL Compose service | Compose |
| `DATABASE_URL` | All PostgreSQL-backed application roles | `DatabaseConfig` |
| `OPENROUTER_API_KEY` | `LLM_PROVIDER=openrouter` | `OpenRouterConfig` |

`JWT_SECRET_KEY` is additionally required for JWT API/web authentication.
Generate at least 32 random bytes, for example with `openssl rand -hex 32`.

`config/ratatoskr.yaml` supplies the checked-in non-secret model, scraper, media,
and runtime configuration. Some model/media fields intentionally have no code
default, so removing that YAML requires supplying equivalent environment
overrides.

## Precedence and secret handling

The loader applies two precedence chains:

```text
non-secret YAML  >  process environment  >  .env / constructor input  >  field default
secret environment                         >  field default
```

`app/config/_secret_marker.py` marks secret fields next to their Pydantic
definition. Secret-marked YAML keys are ignored and logged as
`yaml_secret_keys_ignored`; they must be supplied through `.env`, an injected
process environment, or the deployment's secret manager.

The YAML search order is:

1. the path in `RATATOSKR_CONFIG`;
2. `./ratatoskr.yaml`;
3. `./config/ratatoskr.yaml`;
4. `/app/config/ratatoskr.yaml`.

Only the first existing file is loaded. See [Configuration File](config-file.md).

## Configuration ownership

Use the owning model to find the primary environment alias, type, default,
validator, and whether the field is secret. YAML section names match the
`Settings` attributes.

| Area / YAML section | Owning code |
| --- | --- |
| Telegram, limits, batch delivery | `app/config/telegram.py` |
| LLM budgets, OpenRouter, direct providers, routing | `app/config/llm.py` |
| Runtime, aggregation, streaming, URL worker | `app/config/runtime.py` |
| PostgreSQL | `app/config/database.py` |
| Redis | `app/config/redis.py` |
| API limits, auth, sync | `app/config/api.py` |
| Scraper chain | `app/config/scraper.py` |
| Firecrawl request options | `app/config/firecrawl.py` |
| YouTube and attachments | `app/config/media.py` |
| Transcription | `app/config/transcription.py` |
| Twitter/X extraction and OAuth | `app/config/twitter.py` |
| Web search, MCP, batch analysis, embeddings, Qdrant | `app/config/integrations.py` |
| Digest | `app/config/digest.py` |
| Email | `app/config/email.py` |
| TTS | `app/config/tts.py` |
| RSS and signal ingestion | `app/config/rss.py`, `signal_ingestion.py` |
| Connected social accounts | `app/config/social.py` |
| GitHub ingestion | `app/config/github.py` |
| Git mirrors | `app/config/git_backup.py` |
| X bookmark/wiki ingestion | `app/config/x_bookmarks.py` |
| AI account backup | `app/config/ai_backup.py` |
| Retention and exports/backups | `app/config/retention.py`, `backup.py`, `import_export.py` |
| OpenTelemetry and Sentry | `app/config/otel.py` |
| LangGraph checkpointing | `app/config/langgraph.py` |
| Worker/background capacity | `app/config/background.py`, `worker_capacity.py` |
| Deployment safety and public status probes | `app/config/deployment.py` |

When adding or renaming a setting, update the Pydantic field, checked-in YAML,
Compose role overrides, `.env.example` if it is first-run critical, and the
relevant subsystem guide. Do not maintain a second handwritten list of every
alias here.

## Common environment overrides

These are the most frequently changed application inputs. The owning model is
authoritative for exact validation.

### LLM

| Variable | Purpose |
| --- | --- |
| `LLM_PROVIDER` | `openrouter`, `openai`, `anthropic`, or `ollama`. |
| `OPENROUTER_API_KEY` | Secret for the default provider. |
| `OPENAI_API_KEY` | Secret for the direct OpenAI adapter. |
| `ANTHROPIC_API_KEY` | Secret for the direct Anthropic adapter. |
| `OPENROUTER_MODEL` | Primary OpenRouter model override. |
| `OPENROUTER_FALLBACK_MODELS` | Comma-separated OpenRouter fallback order. |
| `OPENROUTER_FLASH_MODEL` | Lightweight OpenRouter model. |
| `OPENROUTER_LONG_CONTEXT_MODEL` | Long-input OpenRouter model. |
| `SUMMARY_PROMPT_VERSION` | Prompt/cache namespace; not a summary schema version. |
| `LLM_MAX_CALLS_PER_REQUEST` | Hard bound on provider invocations for one request. |

See [LLM Providers](llm-providers.md) for capability differences. Keep model
names in YAML unless a deployment genuinely needs an environment override.

### Database, Redis, and vector search

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | SQLAlchemy `postgresql+asyncpg://...` DSN. |
| `DATABASE_POOL_SIZE` | Async pool size per process. |
| `DATABASE_MAX_OVERFLOW` | Extra connections allowed per process. |
| `REDIS_URL` | Redis connection used by the configured role. |
| `REDIS_PASSWORD` | Password for the bundled Redis service and clients. |
| `REDIS_REQUIRED` | Fail startup when Redis is unavailable. |
| `QDRANT_URL` | Qdrant endpoint. |
| `QDRANT_API_KEY` | Qdrant credential when required. |
| `QDRANT_REQUIRED` | Fail instead of degrading when Qdrant is unavailable. |
| `EMBEDDING_PROVIDER` | Active embedding adapter. |
| `VECTOR_RECONCILE_ENABLED` | Scheduled Postgres/Qdrant convergence. |
| `VECTOR_RECONCILE_CRON` | Reconciliation cron expression. |

Process count multiplies database pools and external-call concurrency. Worker
capacity overrides are documented by `app/config/worker_capacity.py` and the
startup capacity log.

### Scraping and search

| Variable | Purpose |
| --- | --- |
| `SCRAPER_ENABLED` | Generic scraper-chain master switch. |
| `SCRAPER_PROVIDER_ORDER` | Ordered 13-provider token list. |
| `SCRAPER_FORCE_PROVIDER` | Diagnostic single-provider override. |
| `SCRAPER_BROWSER_ENABLED` | Browser-provider master gate. |
| `SCRAPER_RACE_ENABLED` | Race eligible provider tiers instead of serial fallback. |
| `SCRAPER_ALLOW_PRIVATE_NETWORK_URLS` | Local-only SSRF override; keep false in shared/production environments. |
| `FIRECRAWL_SELF_HOSTED_ENABLED` | Enable the self-hosted Firecrawl client. |
| `FIRECRAWL_SELF_HOSTED_URL` | Self-hosted Firecrawl endpoint. |
| `WEB_SEARCH_ENABLED` | Enable enrichment search. |
| `WEBWRIGHT_ENABLED` | First Webwright gate. |
| `WEBWRIGHT_HOST_ALLOWLIST` | Second Webwright gate; empty disables all hosts. |

The active chain and per-provider aliases are documented in
[Scraper Chain](../explanation/scraper-chain.md). Firecrawl cloud credentials are
not used to construct an active client; scraper and search requests use the
self-hosted client when enabled.

### Media and transcription

| Variable | Purpose |
| --- | --- |
| `YOUTUBE_DOWNLOAD_ENABLED` | YouTube platform extractor. |
| `YOUTUBE_STORAGE_PATH` | Download/storage directory. |
| `YOUTUBE_PREFERRED_QUALITY` | Supported yt-dlp quality target. |
| `ATTACHMENT_PROCESSING_ENABLED` | Image/PDF/document processing. |
| `ATTACHMENT_STORAGE_PATH` | Attachment workspace. |
| `TRANSCRIPTION_ENABLED` | Shared speech-to-text adapter. |
| `TRANSCRIPTION_PROVIDER` | `local` or `openai`. |
| `TRANSCRIPTION_API_KEY` | Secret for a remote transcription provider. |
| `TRANSCRIPTION_AUTO_URL_PIPELINE` | Captionless-video transcription fallback. |

See [YouTube](../guides/configure-youtube-download.md) and
[Transcription](../explanation/transcription.md).

### Authentication and external clients

| Variable | Purpose |
| --- | --- |
| `ALLOWED_USER_IDS` | User allowlist; behavior differs by transport when empty. |
| `TELEGRAM_SESSION_DIR` | Persistent Telethon bot-session directory; Compose defaults it to `/data`. |
| `ALLOWED_CLIENT_IDS` | Client identifier allowlist. |
| `AUTH_ALLOW_ANY_CLIENT_ID` | Explicit empty-allowlist override. |
| `JWT_SECRET_KEY` | Current JWT signing secret. |
| `JWT_SECRET_PREVIOUS_KEYS` | Decode-only keys during signing-key rotation. |
| `SECRET_LOGIN_ENABLED` | Client-secret login surface. |
| `AUTH_ARGON2_MAX_CONCURRENCY` | Per-API-process Argon2 admission limit; defaults to `2` to bound memory-hard password/client-secret work. |
| `METRICS_BEARER_TOKEN` | Dedicated 32+ character bearer token shared by mobile-api and Prometheus for `/internal/metrics`; required when the monitoring profile is enabled. |
| `API_RATE_LIMIT_DEFAULT` | Default API rate-limit policy. |
| `API_RATE_LIMIT_AUTH` | Authentication endpoint limit. |

See [Mobile API](mobile-api.md#authentication) and
[Secret Rotation](../runbooks/secret-rotation.md).

### Public status

| Variable | Purpose |
| --- | --- |
| `STATUS_BOT_METRICS_URL` | Internal Telegram bot exporter URL. |
| `STATUS_WORKER_METRICS_URL` | Internal aggregated Taskiq worker exporter URL. |
| `STATUS_SCHEDULER_METRICS_URL` | Internal scheduler exporter URL. |
| `STATUS_NODE_METRICS_URL` | Internal node-exporter URL used for PostgreSQL backup freshness. |
| `STATUS_PROBE_TIMEOUT_SECONDS` | Per-component probe ceiling, at most five seconds. |
| `STATUS_TOTAL_TIMEOUT_SECONDS` | Aggregate status collection ceiling, at most five seconds. |
| `STATUS_CACHE_TTL_SECONDS` | Process-local public status cache, 15–30 seconds. |
| `STATUS_REFRESH_AFTER_SECONDS` | Suggested client refresh interval, 15–300 seconds. |

The primary Compose file supplies the four exporter URLs to `mobile-api`.
They are credential-free internal HTTP URLs and are never returned by the
public API. See [Status page and system metrics](status-page.md).

### Optional integrations

Each optional integration has its own feature gate and credentials. Common
groups include `DIGEST_*`, `EMAIL_*`/`SMTP_*`/`RESEND_*`, `ELEVENLABS_*`,
`GITHUB_*`, `X_*`, `THREADS_*`, `INSTAGRAM_*`, `RSS_*`, `SIGNAL_*`,
`X_BOOKMARKS_*`, `GIT_BACKUP_*`, and `AI_BACKUP_*`.

Do not enable a group by copying every variable. Start from its subsystem guide
and set only the feature gate, required credentials, and intended overrides.

## Compose-only deployment variables

Some values are consumed by Compose or sidecars rather than `Settings`:

| Variable | Consumer |
| --- | --- |
| `POSTGRES_PASSWORD` | Bundled PostgreSQL, application DSN construction, backup sidecar. |
| `POSTGRES_HOST_PORT`, `REDIS_HOST_PORT`, `QDRANT_HOST_PORT` | Development port overrides. |
| `BACKUP_HOST_DIR`, `BACKUP_CRON`, `BACKUP_RUN_ON_START` | PostgreSQL backup sidecar. |
| `BACKUP_REQUIRE_ENCRYPTION`, `BACKUP_S3_*` | Backup-sidecar policy/off-host copy. |
| `GRAFANA_ADMIN_PASSWORD` | Monitoring profile. |
| `ALERT_WEBHOOK_URL` | Generic Alertmanager webhook receiver. HTTP is supported for trusted internal delivery; use HTTPS for external delivery. |
| `ALERT_SLACK_API_URL` | HTTPS Slack incoming-webhook URL rendered into the active Alertmanager receiver. |
| `ALERT_TELEGRAM_WEBHOOK_URL` | HTTPS endpoint that accepts Alertmanager webhook payloads and delivers them to Telegram. |
| `ALERT_PAGERDUTY_ROUTING_KEY` | PagerDuty Events API v2 integration routing key rendered into the active Alertmanager receiver. |
| `RATATOSKR_DOCKER_NETWORK` | Existing core network joined by the standalone monitoring Compose file; defaults to `docker_default`. |
| `RATATOSKR_PG_BACKUP_METRICS_VOLUME` | Existing core backup textfile volume mounted by standalone node-exporter; defaults to `docker_pg_backup_metrics`. |
| `COMPOSE_PROFILES` | Optional scraper, monitoring, MCP, and related service groups. |

When `RATATOSKR_ENV=production`, Alertmanager fails startup unless at least one
of the four receiver variables is valid. Multiple configured variables fan out
the same alert to every configured integration. Receiver values are written
only to the container-private rendered config and must not be logged.

Inspect the effective deployment after substitution:

```bash
POSTGRES_PASSWORD=... \
docker compose -f ops/docker/docker-compose.yml config
```

## Deprecated names rejected at startup

The loader fails with a replacement hint when these old scraper names are set:

| Removed | Replacement |
| --- | --- |
| `SCRAPLING_ENABLED` | `SCRAPER_SCRAPLING_ENABLED` |
| `SCRAPLING_TIMEOUT_SEC` | `SCRAPER_SCRAPLING_TIMEOUT_SEC` |
| `SCRAPLING_STEALTH_FALLBACK` | `SCRAPER_SCRAPLING_STEALTH_FALLBACK` |
| `SCRAPER_DIRECT_HTTP_ENABLED` | `SCRAPER_DIRECT_HTML_ENABLED` |

The removed `MIGRATION_SHADOW_MODE_*` controls are also rejected. Delete them;
they have no active replacement surface.

## Validate configuration

Use the Python 3.13 project environment and the same environment/YAML that the
target role will receive:

```bash
uv run python -c \
  'from app.config import load_config; load_config(); print("configuration valid")'
```

Then validate dependencies rather than only parsing settings:

```bash
uv run python -m app.cli.migrate_db --check
redis-cli -u "$REDIS_URL" ping
curl --fail "$QDRANT_URL/healthz"
```

Never print the loaded config object in production diagnostics; it contains
secret-bearing fields. Use the owner-only diagnostics surfaces and redacted logs.

See [Troubleshooting: configuration](troubleshooting.md#configuration-issues) for
common failures.
