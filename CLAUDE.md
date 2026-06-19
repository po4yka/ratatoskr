# CLAUDE.md -- AI Assistant Guide for Ratatoskr

Operating notes for Claude (and other AI assistants) working in this repo. Leads with non-obvious rules and gotchas; defers reference material to `docs/`, `.claude/skills/`, and the Codex mirror under `.codex/skills/`.

## Workspace skills

Cross-repo skills (`openapi-bump-cross-repo`, `local-stack-up`, `frost-token-mirror`, `workspace-status`) live in the parent workspace at `../.claude/skills/`. To pick them up when working inside this repo, launch Claude with `claude --add-dir ..`. For Codex, use `.codex/skills/` in this repo and copy or mirror any cross-repo skill into a Codex skill root before treating it as available. See `../CLAUDE.md` for the workspace overview and the cross-repo OpenAPI contract.

## Project Overview

**Ratatoskr** is an async, single-tenant Telegram bot that:

- Summarizes web articles, YouTube videos, Twitter/X posts, GitHub repos, and academic papers via a multi-provider scraper chain + OpenRouter by default, with direct `openai`, `anthropic`, and `ollama` LLM adapters available through `LLM_PROVIDER`.
- Returns structured JSON summaries against a strict contract.
- Persists every artifact -- Telegram messages, scraper responses, LLM calls (success and failure), summaries, embeddings -- in PostgreSQL via SQLAlchemy 2.0 + asyncpg.
- Ships as a single Docker container with an owner-only access whitelist.

**Core stack:** Python 3.13, Telethon, SQLAlchemy 2.0 + asyncpg, OpenRouter/direct LLM adapters, Qdrant (with sentence-transformers or Gemini Embedding 2), FastAPI + JWT (Mobile API), React + TypeScript + Vite (web frontend). Full dependency list lives in `pyproject.toml`.

## Architecture & Docs Index

`docs/SPEC.md` is the navigation hub. Reach for these first instead of guessing:

| Need | Doc |
|---|---|
| Bird's-eye view, request lifecycle | `docs/explanation/architecture-overview.md` |
| Multi-agent architecture | `docs/explanation/multi-agent-architecture.md` |
| Scraper-chain fallback design | `docs/explanation/scraper-chain.md` |
| Webwright browser-agent integration (sidecar, `/browse`, enricher) | `docs/explanation/webwright.md` |
| Why the summary contract is shaped that way | `docs/explanation/summary-contract-design.md` |
| GitHub repo ingestion subsystem | `docs/explanation/github-repository-ingestion.md` |
| On-disk git mirroring (git backup) | `docs/explanation/git-mirroring.md` |
| fieldtheory-cli integration (bookmarks, wiki, MCP search, Telegram) | `docs/explanation/x-bookmarks-integration.md` |
| Vector index sync | `docs/vector-index-sync.md` |
| Authoritative env-var reference (820 lines) | `docs/reference/environment-variables.md` |
| Authoritative DB schema | `docs/reference/data-model.md` |
| Summary JSON contract spec | `docs/reference/summary-contract.md` |
| Channel digest ops | `docs/reference/digest-subsystem-ops.md` |
| Common failure recipes | `docs/reference/troubleshooting.md` |
| Claude Code safety hooks | `docs/reference/claude-code-hooks.md` |

## Secrets

Secret rotation and drill procedures live in `docs/runbooks/secret-rotation.md`. Use that runbook for `GITHUB_TOKEN_ENCRYPTION_KEY`, `JWT_SECRET_KEY`, `BOT_TOKEN`, `BACKUP_ENCRYPTION_KEY`, MCP forwarding secrets, provider API keys, and login peppers; do not rely on inline code comments as the operational procedure.

## Agent Implementation Map

| Need | Implementation map |
|---|---|
| Auth/session failure | Backend auth is split across `app/api/routers/auth/` transport handlers, `app/api/routers/auth/tokens.py` token creation/validation, `app/api/routers/auth/cookies.py` refresh-cookie policy, `app/infrastructure/persistence/repositories/auth_repository.py` persistence, and `app/db/models/core.py::RefreshToken`; token TTL/storage behavior is documented in `docs/reference/mobile-api.md#authentication-modes` and failure triage starts at `docs/reference/troubleshooting.md#refresh-token-stops-working`. |
| API contract drift | FastAPI source is `app.api.main:app`; request/response models live under `app/api/models/`, routers under `app/api/routers/`, generated artifacts under `docs/openapi/mobile_api.yaml` and `.json`; never patch generated OpenAPI by hand. Run `make generate-openapi`, `make check-openapi-drift`, `make check-openapi-validate`, and `make check-openapi`. |
| Sync drift | Sync v2 entrypoints are `app/api/routers/sync.py`, service collaborators are under `app/api/services/sync/`, DB reads for sync live in `app/infrastructure/persistence/sync_aux_read_adapter.py`, and the contract map is `docs/reference/sync-protocol.md`. |
| Request stuck in processing | The summarize graph is the sole path: start with the URL-flow facade `app/adapters/content/graph_url_processor.py` (`GraphURLProcessor.handle_url_flow`), the graph spine `app/application/graphs/summarize/` (`graph.py` + `nodes/`, especially `ingest`/`extract`/`persist`/`notify`), `app/adapters/content/platform_extraction/lifecycle.py`, `app/db/models/core.py::RequestProcessingJob`, and `docs/reference/troubleshooting.md#request-stuck-in-processing`; keep correlation IDs intact across any repair (`thread_id == correlation_id`). |
| LLM parse failure | Validation + repair are graph nodes: `app/application/graphs/summarize/nodes/validate.py` and `repair.py`, backed by `app/application/services/summarization/llm_response_workflow_attempts.py` + `llm_response_workflow_repair.py`, `app/application/services/summarization/graph_llm.py` (`summarize_with_instructor`), and `app/core/summary_contract.py`; runtime prompt/schema binding is `SummaryContractDescriptor` plus `PromptManager.get_contract_system_prompt()`; the validate → repair ↺ validate loop is bounded by `MAX_REPAIR_ATTEMPTS` (`app/application/graphs/summarize/state.py`) and langgraph's per-invocation `recursion_limit`; failure recipe is `docs/reference/troubleshooting.md#json-parsing-failures`. |
| Extraction provider behavior | Generic URL extraction is `app/adapters/content/scraper/` plus `app/adapters/content/platform_extraction/`; platform-specific bypasses are `app/adapters/youtube/`, `app/adapters/twitter/`, and `app/adapters/academic/`; provider docs are `docs/explanation/scraper-chain.md`. |
| Source ingestion and vector repair | Source ingestors live in `app/adapters/ingestors/`, RSS/digest helpers in `app/adapters/rss/` and `app/adapters/digest/`, signal API in `app/api/routers/social/signals.py`, vector reconciliation in `app/infrastructure/vector/reconciliation.py`, `app/cli/reconcile_vector_index.py`, and `app/cli/backfill_vector_store.py`; new vectorized entity types should implement `VectorIndexedEntityAdapter`; vector drift docs are `docs/vector-index-sync.md`. |
| On-disk git mirroring (git backup) | Engine and service: `app/adapters/git_backup/` (`mirror_service.py` = `GitMirrorService`, `repository.py` = `GitMirrorRepository`). Config: `app/config/git_backup.py` (`GitBackupConfig`). DB model: `app/db/models/git_backup.py` (`GitMirror`). Scheduled Taskiq job: `app/tasks/git_backup_sync.py` (task name `ratatoskr.git_backup.sync`, Redis-locked, cron `GIT_BACKUP_SYNC_CRON`). Telegram commands `/mirror` and `/mirrors`: `app/adapters/telegram/command_handlers/git_mirror_handler.py`. REST endpoints `GET/POST/DELETE /v1/git-mirrors`: `app/api/routers/git_mirrors.py`. **Distinct from the GitHub API-based metadata ingestion** (`app/adapters/github/`, `app/tasks/github_sync.py`) which never clones to disk — that path fetches repo metadata and indexes it in PostgreSQL + Qdrant. Git backup performs actual `git clone --mirror` of full history to `GIT_BACKUP_DATA_PATH` and reuses `GITHUB_TOKEN_ENCRYPTION_KEY` for authenticated clones. |

## Directory Structure

```
app/
+-- adapters/           # External service integrations
|   +-- academic/       # arXiv, SSRN, NBER, OSF, ResearchGate, RePEc handler
|   +-- attachment/     # Attachment processing
|   +-- content/        # URL processing pipeline
|   |   +-- scraper/    # Multi-provider scraper chain (protocol, chain, factory, providers)
|   |   +-- streaming/  # In-process StreamHub pub/sub (feeds SSE + Telegram drafts)
|   +-- digest/         # Channel digest userbot, channel reader, analyzer
|   +-- elevenlabs/     # ElevenLabs TTS
|   +-- external/       # Firecrawl parser, response formatter facade
|   +-- git_backup/     # On-disk git mirror engine (GitMirrorService, GitMirrorRepository, LFS, maintenance, circuit breaker)
|   +-- github/         # GitHub API integration and repository ingestion
|   +-- ingestors/      # Source-ingestor framework
|   +-- llm/            # LLM abstraction + direct OpenAI-compatible / Anthropic adapters
|   +-- meta/           # Meta adapter helpers
|   +-- openrouter/     # OpenRouter client and helpers
|   +-- rss/            # RSS polling and feed helpers
|   +-- social/         # Social connection adapters
|   +-- stt/            # Speech-to-text adapter boundary
|   +-- telegram/       # Bot logic, command_handlers/, access controller
|   +-- transcription/  # Transcription service adapters
|   +-- twitter/        # Twitter/X two-tier extractor (Firecrawl + Playwright)
|   +-- video/          # Video pipeline adapters
|   +-- webwright/      # Microsoft Webwright sidecar adapter (client) — used by scraper chain + /browse
|   +-- youtube/        # yt-dlp + transcript extraction
+-- agents/             # Classic agents (web search, repo analysis, multi-source aggregation)
+-- api/                # Mobile API (FastAPI, JWT, sync, collections, streams, digest, ...)
|   +-- routers/auth/   # Auth router package: endpoints_sessions.py, endpoints_credentials.py, endpoints_secret_keys.py, endpoints_telegram.py, github.py, tokens.py, cookies.py
+-- application/        # DDD application layer (DTOs, use cases)
+-- config/             # Settings, scraper config, runtime tuning
+-- core/               # URL utils, JSON utils, summary contract descriptors/schema, lang detection
+-- db/                 # Models + Database session manager + Alembic migrations
+-- di/                 # Runtime composition
+-- domain/             # Domain models and services
+-- infrastructure/     # Persistence, cache, vector store, embedding
+-- mcp/                # Model Context Protocol server
+-- observability/      # Prometheus metrics (metrics.py), OTel tracing (otel.py: provider/exporters/Telethon helper), ratatoskr.* span-attribute constants (attributes.py)
+-- prompts/            # LLM system prompts (en/ru, summary / combined_summary / instructor)
+-- security/           # Token crypto (Fernet), redaction
+-- tasks/              # Taskiq tasks (github_sync, reconcile_vector_index, digest, ...)
```

Skill, doc, and ops trees: `.claude/skills/` (Claude project skills), `.codex/skills/` (Codex project skills), `.codex/commands/` (Codex command prompts), `docs/` (explanation + reference), `ops/` (Docker / monitoring / config), `tools/scripts/` (dev utilities).

## Operating Rules

Project-specific conventions that aren't visible from code alone. Treat these as load-bearing.

1. **Correlation IDs are sacred.** Every request gets a `correlation_id`; every user-visible error must include `Error ID: <correlation_id>`; every log line and DB row touching that request carries it. Don't strip it, don't regenerate it mid-flow.
2. **URLs must be normalized before deduplication.** `app/core/url_utils.py` is the single normalizer. `dedupe_hash` (sha256 of the normalized URL) is the idempotence key.
3. **Persist everything.** Scraper responses → `crawl_results`, LLM calls (success AND failure) → `llm_calls`, Telegram messages → `telegram_messages`, summaries → `summaries`. Observability is non-negotiable -- if a step can fail, the failure goes in the DB.
4. **Redact `Authorization` headers before persistence and before logging.** Helpers already do this; don't bypass them.
5. **`Database` (`app/db/session.py`) is the sole DB entry point.** Don't open ad-hoc `AsyncSession`s in adapters.
6. **Async only.** Telethon + httpx + asyncpg + SQLAlchemy `AsyncSession`. No blocking calls in the request path; use `asyncio.to_thread` only for genuinely sync libs.
7. **Update both `en` and `ru` prompts together.** Files under `app/prompts/` come in mirrored pairs (`summary_system_en.txt` / `summary_system_ru.txt`, etc.); changing one without the other silently breaks the other-language path.
8. **YouTube, Twitter/X, and academic papers each have dedicated extractors** (`app/adapters/youtube/`, `twitter/`, `academic/`) that bypass the standard scraper chain. Check `requests.source_kind` before assuming the chain ran.
9. **Webwright is the only chain rung that costs real LLM money per URL.** Default off (`WEBWRIGHT_ENABLED=false`) and double-gated by a non-empty `WEBWRIGHT_HOST_ALLOWLIST` — an empty allowlist short-circuits provider construction so the sidecar is never called. The same sidecar also serves `/browse` (Path B); the former enricher (Path C) has been removed. Design rationale lives in `docs/explanation/webwright.md`.
10. **When a client ships a new default client_id, add it to `app/config/known_client_ids.py` `KNOWN_CLIENT_IDS` and to every deployment's `ALLOWED_CLIENT_IDS` env var (or set `AUTH_ALLOW_ANY_CLIENT_ID=true` for local/development deployments).**
11. **Model selection has no code default -- `ratatoskr.yaml` is the single source of truth.** `OpenRouterConfig.model`/`fallback_models`/`flash_model`/`flash_fallback_models`/`long_context_model` and `AttachmentConfig.vision_model`/`vision_fallback_models` are required fields with no `Field(default=...)` for the default OpenRouter path. Direct `openai`, `anthropic`, and `ollama` modes require their matching provider model fields instead. When changing models, edit the matching provider section of `config/ratatoskr.yaml` (and the deployed `/app/config/ratatoskr.yaml`) -- never re-add code defaults. Tests that build config under `patch.dict(..., clear=True)` must supply these via `tests/_config_env.py::MODEL_SELECTION_ENV` for OpenRouter paths.
12. **`user_id` WHERE filters are a defense-in-depth IDOR guard -- never remove them, even though the bot is single-tenant.** A single-owner deployment makes the predicate `WHERE user_id = <constant>` look redundant, but dropping it creates a forward-looking IDOR: any second authenticated identity (JWT-secret compromise, a manually-added DB row, or future multi-tenancy) would silently read all rows. An audit removed these filters once (`ae5c8b08`) and they were restored the same day (`26375553`). The retired single-tenant-simplification ADR that proposed removing them was deleted on 2026-06-15 because the project chose expansion over simplification; this rule preserves its one load-bearing conclusion.

### Bugbear rules to never suppress project-wide

| Rule | What it catches | Safe fix |
|---|---|---|
| **B006** `mutable-argument-default` | `def f(x=[])` -- shared mutable default leaks state across calls | `None` sentinel + in-body init |
| **B023** `function-uses-loop-variable` | `lambda: key` inside a loop -- all closures see the final value | `lambda key=key: key`, factory fn, or `functools.partial` |

Narrow inline `# noqa: B023` with a justification comment is OK in tests; file-level ignore is not.

### Docker image-name footgun (Pi deploy)

Two image names are in play:

- `make docker-deploy` (legacy) builds `ratatoskr:latest` via `docker build`.
- `docker compose build` produces `ratatoskr-ratatoskr` (compose prefixes the project name).

`docker compose up` uses the second. Building with `docker build -t ratatoskr:latest` and then `docker compose up` does NOT pick up your code changes. **Always deploy via `make pi-deploy` or `docker compose -f ops/docker/docker-compose.yml build ratatoskr`** -- never `docker build` directly. The base compose file deliberately does NOT bind-mount `app/`; re-adding that mount would silently mask `make pi-deploy`.

For local hot-reload (Mac only, never on the Pi): add `ops/docker/docker-compose.dev.yml` as an overlay.

## Project Skills

Task-oriented skills under `.claude/skills/` and `.codex/skills/`. Each carries its own workflow, trigger keywords, and dynamic context (live DB queries). Reach for these instead of re-deriving the steps.

| Skill | Use when |
|---|---|
| `adding-telegram-command` | adding a new `/foo` slash command (handler + registry wiring) |
| `alembic-migrations` | adding or modifying SQLAlchemy models / Postgres schema |
| `inspecting-database` | querying Postgres for requests, summaries, LLM calls, crawl results |
| `debugging-apis` | Firecrawl / OpenRouter request-response inspection, retry / cost / rate-limit triage |
| `validating-summaries` | summary JSON contract checks, character-limit failures |
| `langgraph-summarize-loop` | summarize-graph node walk + retry / repair-loop / `attempt_trigger` debugging (the graph is the sole summarize path; `attempt_trigger='graph_node'`) |
| `vector-index-sync` | Qdrant + `summary_embeddings` + reconciler (fast path + Taskiq backfill) |
| `scraper-chain-debugging` | content-scraper fallback chain failures (now includes the Webwright rung) |
| `digest-subsystem-ops` | channel digest userbot, `/init_session` flow, scheduling |
| `pi-deploy` | building and shipping the image to the Raspberry Pi |
| `web-frontend-dev` | React + TypeScript + Vite work under `web/` |
| `testing-workflows` | CLI runner, message simulation, pytest patterns |
| `repo-task-board` | task creation / status transitions in `docs/tasks/` |

## Common Commands

```bash
# Setup
make venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# Quality gates
make format          # ruff format + isort
make lint            # ruff
make type            # mypy

# Dependencies
make lock-uv         # lock with uv (recommended)

# Docker (compose is the source of truth; never use raw `docker build`)
docker compose -f ops/docker/docker-compose.yml build ratatoskr
docker compose -f ops/docker/docker-compose.yml down && \
docker compose -f ops/docker/docker-compose.yml up -d

# Pi deployment (cross-compile linux/arm64 on Mac, stream to Pi, restart)
make pi-deploy                        # build + ship + restart `ratatoskr`
make pi-deploy SERVICE=mobile-api     # mobile-api image instead
make pi-deploy-no-cache               # full rebuild
make pi-build-only                    # ship without restarting
bash tools/scripts/build-and-deploy-pi.sh --help   # full flag coverage

# CLI Summary Runner (test pipeline without Telegram)
python -m app.cli.summary --url https://example.com/article
python -m app.cli.summary --accept-multiple --json-path out.json --log-level DEBUG

# DB inspection
docker exec -it ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr
python -m app.cli.migrate_db          # apply Alembic migrations
```

## Code Standards & CI

- **Formatting:** ruff format + isort (profile=black). Pre-commit runs ruff → isort → mypy.
- **Linting:** ruff (see `pyproject.toml`); B006 and B023 enforced (see Operating Rules). `pyproject.toml` ignores `ASYNC240` (added in ruff 0.7+); **the project requires ruff ≥0.15.13** (pinned in `requirements-dev.txt`). Older globally-installed ruff binaries (e.g. `~/.local/bin/ruff` from a stale pipx install) will fail with `Unknown rule selector: ASYNC240`. Upgrade with `pipx upgrade ruff` or always invoke ruff from the project venv.
- **Types:** mypy, `python_version = "3.13"`.
- **Testing:** pytest + pytest-asyncio, hypothesis, pytest-benchmark. Use `tests/db_helpers_async.py` (`create_request`, `insert_summary`, ...) instead of writing fresh fixtures or calling ORM models directly. E2E tests gated by `E2E=1`.
- **CI** (`.github/workflows/ci.yml`): lockfile freshness, lint+format+type, unit tests with a 65% coverage floor (80% target; enforced via `fail_under` in `pyproject.toml`), OpenAPI validation, radon complexity, security (Bandit, pip-audit, Safety, Gitleaks), frontend (`web-build`, `web-test`, `web-static-check`), Docker image build. Optional GHCR publish on `PUBLISH_DOCKER=true`.

## Key File References

| File | Role |
|---|---|
| `bot.py` | Entrypoint -- wires everything |
| `app/adapters/telegram/message_router.py` | Central routing |
| `app/adapters/content/graph_url_processor.py` | URL-flow facade (`GraphURLProcessor`) -- sole entrypoint, delegates to the summarize graph |
| `app/application/graphs/summarize/` | Summarize `StateGraph`: `graph.py` (assembly/invocation) + `nodes/` (ingest → extract → ground → build_prompt → summarize → validate → repair → enrich → persist → notify) |
| `app/adapters/content/scraper/` | Scraper protocol, chain, factory, providers |
| `app/core/summary_contract.py` | Summary contract descriptors and strict validation |
| `app/core/summary_schema.py` | Pydantic model for the contract |
| `app/core/url_utils.py` | URL normalization + `dedupe_hash` |
| `app/db/session.py` | `Database` -- sole DB entry point |
| `app/db/models/` | SQLAlchemy 2.0 typed models, grouped by area |
| `app/config/settings.py` | Configuration loading |
| `app/application/services/summarization/graph_llm.py` | `summarize_with_instructor` -- structured-output LLM call shared by the summarize + repair nodes |
| `app/api/main.py` | Mobile API entrypoint |
| `app/di/tasks.py` | Taskiq runtime dependency bundles for digest, RSS/source ingestion, and vector reconciliation |
| `app/mcp/server.py` | MCP server for AI agents |
| `docs/SPEC.md` | Documentation navigation hub |

## Database Models

SQLAlchemy 2.0 typed declarative models registered in `ALL_MODELS` (`app/db/models/__init__.py`), grouped by area:

| Module | Models |
|---|---|
| `core.py` | `User`, `Chat`, `Request`, `TelegramMessage`, `CrawlResult`, `LLMCall`, `Summary`, `UserInteraction`, `AuditLog`, `SummaryEmbedding`, `VideoDownload`, `AudioGeneration`, `AttachmentProcessing`, `UserDevice`, `RefreshToken`, `ClientSecret` |
| `aggregation.py` | `AggregationSession`, `AggregationSessionItem` |
| `batch.py` | `BatchSession`, `BatchSessionItem` |
| `collections.py` | `Collection`, `CollectionItem`, `CollectionCollaborator`, `CollectionInvite` |
| `digest.py` | `Channel`, `ChannelCategory`, `ChannelSubscription`, `ChannelPost`, `ChannelPostAnalysis`, `DigestDelivery`, `UserDigestPreference` |
| `repository.py` | `Repository`, `RepositoryEmbedding`, `UserGitHubIntegration` |
| `rss.py` | `RSSFeed`, `RSSFeedSubscription`, `RSSFeedItem`, `RSSItemDelivery` |
| `rules.py` | `WebhookSubscription`, `WebhookDelivery`, `AutomationRule`, `RuleExecutionLog`, `ImportJob`, `UserBackup` |
| `signal.py` | `Source`, `Subscription`, `FeedItem`, `Topic`, `UserSignal` |
| `topic_search.py` | `TopicSearchIndex` (Postgres TSVECTOR + GIN) |
| `user_content.py` | `SummaryFeedback`, `CustomDigest`, `SummaryHighlight`, `UserGoal`, `Tag`, `SummaryTag` |
| `webwright.py` | `WebwrightRun`, `UserBrowserSession` (Fernet-encrypted per-domain cookies; reuses `GITHUB_TOKEN_ENCRYPTION_KEY`) |
| `git_backup.py` | `GitMirror` (bare clone state: URL, source kind, last sync, mirror path); enums `GitMirrorSource`, `GitMirrorStatus` |

`LLMCall` rows carry `attempt_index` (1-based, monotonic per `request_id`) and `attempt_trigger` (Postgres enum: `initial`, `user_retry`, `auto_backfill`, `repair_loop`, `stream_fallback_retry`, `webwright_tool`, `graph_node`) so retries, the repair loop, and graph-node LLM calls are queryable without timestamp inference. Since the T9 cutover the summarize graph is the sole summarize path, so its summarize + repair node calls are written with `attempt_trigger='graph_node'` (the active value); `webwright_tool` and `stream_fallback_retry` remain reserved.

Schema and migration workflow: `alembic-migrations` skill + `docs/reference/data-model.md`.

## Environment Variables

Full reference (820 lines): `docs/reference/environment-variables.md`. Load-bearing ones:

| Var | Purpose |
|---|---|
| `ALLOWED_USER_IDS` | Comma-separated Telegram user IDs allowed to use the bot |
| `LLM_PROVIDER` | `openrouter` (default), `openai`, `anthropic`, or `ollama`; see `docs/guides/configure-llm-provider.md` and `docs/reference/llm-providers.md` |
| `OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` | LLM provider keys (secret -- `.env` only); only the selected provider's key is required |
| `OPENROUTER_MODEL`, `OPENROUTER_FALLBACK_MODELS`, `OPENROUTER_FLASH_MODEL`, `OPENROUTER_FLASH_FALLBACK_MODELS`, `OPENROUTER_LONG_CONTEXT_MODEL`, `OPENAI_MODEL`, `ANTHROPIC_MODEL`, `OLLAMA_MODEL`, `ATTACHMENT_VISION_MODEL`, `ATTACHMENT_VISION_FALLBACK_MODELS` | **Model selection -- no code default for selected provider.** Must be set in `ratatoskr.yaml` or env for the selected provider; `ratatoskr.yaml` is the single source of truth for which models the service uses. |
| `LLM_CALL_TIMEOUT_SEC`, `LLM_PER_MODEL_TIMEOUT_MIN_SEC`, `LLM_PER_MODEL_TIMEOUT_OVERRIDES` | LLM cascade budget shaping |
| `DATABASE_URL` | Postgres DSN |
| `DIGEST_ENABLED`, `API_BASE_URL` | Channel digest subsystem on/off + Mini App callback URL |
| `GITHUB_TOKEN_ENCRYPTION_KEY` | Fernet key for at-rest GitHub PAT / OAuth tokens |
| `EMBEDDING_PROVIDER` | `local` (sentence-transformers) or `gemini` -- switching invalidates all existing vectors |
| `VECTOR_RECONCILE_ENABLED` | Taskiq reconciler for vector-index convergence/backfill (default `true`). The summarize graph's persist node writes a read-your-writes Qdrant point synchronously (byte-identical via `app/infrastructure/vector/summary_point.py`) so a new summary is retrievable immediately; the reconciler closes any gaps on its 30-minute cadence (ADR-0012). See `docs/vector-index-sync.md`. |
| `SUMMARIZE_RAG_ENABLED`, `RAG_TOP_K` | RAG grounding in the summarize graph's `ground` node (default off): retrieve top-k scope-filtered prior summaries via the unified retrieval port + inject an anti-contamination "related prior summaries (reference only)" block into the system prompt (ADR-0005/0012/0016). Transitional flag, retired at the T6 cutover. Embedding models stay in `ratatoskr.yaml`. |
| `X_BOOKMARKS_SYNC_ENABLED`, `X_BOOKMARKS_SYNC_CRON`, `X_WIKI_SYNC_CRON` | Master switch + cron for the two x_bookmarks delta-scan Taskiq jobs (bookmark + wiki). Both jobs share the `enabled` flag. |
| `X_BOOKMARKS_DB_PATH`, `X_WIKI_LIBRARY_PATH`, `X_IDEAS_PATH` | Container-side paths to the host-mounted `~/.fieldtheory/` subtrees (`bookmarks.db`, `library/`, `ideas/`). Defaults: `/x_bookmarks/...` — bind-mounted read-only by the operator. |
| `WEBWRIGHT_ENABLED`, `WEBWRIGHT_HOST_ALLOWLIST`, `WEBWRIGHT_URL`, `WEBWRIGHT_MAX_STEPS`, `WEBWRIGHT_TIMEOUT_SEC`, `WEBWRIGHT_MODEL` | Microsoft Webwright sidecar (compose profile `with-webwright`). Heavy: each invocation ~10-30× a normal scrape. Default off; double-gated by feature flag + non-empty host allowlist. See `docs/explanation/webwright.md`. |
| `GIT_BACKUP_ENABLED`, `GIT_BACKUP_SYNC_CRON`, `GIT_BACKUP_DATA_PATH` | On-disk git mirror subsystem. Master switch (default `false`), Taskiq cron schedule (default `0 4 * * *` UTC), and local filesystem path for bare clones (default `/data/git-mirrors`). Full variable set in `docs/reference/environment-variables.md`. |
| `OTEL_ENABLED`, `OTEL_TRACES_EXPORTER`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_FILE_EXPORTER_PATH` | OpenTelemetry tracing. Opt-in master switch (default `false`); span exporter `otlp` (Tempo, default) / `console` / `file` (swap needs no code change); OTLP endpoint (default `http://tempo:4317`); file-exporter path for ad-hoc DuckDB/Polars analysis. 100% sampling is hard-wired (`OTEL_SAMPLE_RATIO` is a documented no-op). Full set + behavior in `docs/reference/environment-variables.md#observability--opentelemetry-tracing`. |

## Task Board

Tasks live as Obsidian Tasks-compatible Markdown lines inside per-task notes. Source of truth: `docs/tasks/issues/<slug>.md`. The `repo-task-board` skill carries the full workflow (templates, transitions, lifecycle); only the canonical layout is repeated here:

- `docs/tasks/issues/<slug>.md` -- one note per task (YAML frontmatter + canonical `- [ ]` line + spec). **Delete the file on close** -- git history is the audit trail.
- `docs/tasks/active.md`, `backlog.md`, `blocked.md`, `dashboard.md` -- Obsidian Tasks query views, NOT task storage. Do not add task lines to these files.
- `docs/tasks/board.md` -- Kanban visualization.

When implementing a task, also update any CLAUDE.md or skill content that the change makes stale, and commit both together.

---

**Last Updated:** 2026-06-18

Reading order for orientation: this file → `docs/SPEC.md` → relevant `docs/explanation/*.md` or `docs/reference/*.md` → matching `.claude/skills/<name>/SKILL.md` or `.codex/skills/<name>/SKILL.md` for Codex sessions.
