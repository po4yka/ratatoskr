# CLAUDE.md -- AI Assistant Guide for Ratatoskr

Operating notes for Claude (and other AI assistants) working in this repo. Leads with non-obvious rules and gotchas; defers reference material to `docs/` and `.claude/skills/`.

## Workspace skills

Cross-repo skills (`openapi-bump-cross-repo`, `local-stack-up`, `frost-token-mirror`, `workspace-status`) live in the parent workspace at `../.claude/skills/`. To pick them up when working inside this repo, launch Claude with `claude --add-dir ..`. See `../CLAUDE.md` for the workspace overview and the cross-repo OpenAPI contract.

## Project Overview

**Ratatoskr** is an async, single-tenant Telegram bot that:

- Summarizes web articles, YouTube videos, Twitter/X posts, GitHub repos, and academic papers via a multi-provider scraper chain + OpenRouter LLMs.
- Returns structured JSON summaries against a strict contract.
- Persists every artifact -- Telegram messages, scraper responses, LLM calls (success and failure), summaries, embeddings -- in PostgreSQL via SQLAlchemy 2.0 + asyncpg.
- Ships as a single Docker container with an owner-only access whitelist.

**Core stack:** Python 3.13, Telethon, SQLAlchemy 2.0 + asyncpg, OpenRouter, Qdrant (with sentence-transformers or Gemini Embedding 2), FastAPI + JWT (Mobile API), LangGraph (summarize/repair retry graph), React + TypeScript + Vite (web frontend). Full dependency list lives in `pyproject.toml`.

## Architecture & Docs Index

`docs/SPEC.md` is the navigation hub. Reach for these first instead of guessing:

| Need | Doc |
|---|---|
| Bird's-eye view, request lifecycle | `docs/explanation/architecture-overview.md` |
| Multi-agent / LangGraph retry loop | `docs/explanation/multi-agent-architecture.md` |
| Scraper-chain fallback design | `docs/explanation/scraper-chain.md` |
| Why the summary contract is shaped that way | `docs/explanation/summary-contract-design.md` |
| GitHub repo ingestion subsystem | `docs/explanation/github-repository-ingestion.md` |
| fieldtheory-cli integration (bookmarks, wiki, MCP search, Telegram) | `docs/explanation/fieldtheory-integration.md` |
| Vector index + CocoIndex sync | `docs/cocoindex.md` |
| Authoritative env-var reference (820 lines) | `docs/reference/environment-variables.md` |
| Authoritative DB schema | `docs/reference/data-model.md` |
| Summary JSON contract spec | `docs/reference/summary-contract.md` |
| Channel digest ops | `docs/reference/digest-subsystem-ops.md` |
| Common failure recipes | `docs/reference/troubleshooting.md` |
| Claude Code safety hooks | `docs/reference/claude-code-hooks.md` |

## Agent Implementation Map

| Need | Implementation map |
|---|---|
| Auth/session failure | Backend auth is split across `app/api/routers/auth/` transport handlers, `app/api/routers/auth/tokens.py` token creation/validation, `app/api/routers/auth/cookies.py` refresh-cookie policy, `app/infrastructure/persistence/repositories/auth_repository.py` persistence, and `app/db/models/core.py::RefreshToken`; token TTL/storage behavior is documented in `docs/reference/mobile-api.md#authentication-modes` and failure triage starts at `docs/reference/troubleshooting.md#refresh-token-stops-working`. |
| API contract drift | FastAPI source is `app.api.main:app`; request/response models live under `app/api/models/`, routers under `app/api/routers/`, generated artifacts under `docs/openapi/mobile_api.yaml` and `.json`; never patch generated OpenAPI by hand. Run `make generate-openapi`, `make check-openapi-drift`, `make check-openapi-validate`, and `make check-openapi`. |
| Sync drift | Sync v2 entrypoints are `app/api/routers/sync.py`, service collaborators are under `app/api/services/sync/`, DB reads for sync live in `app/infrastructure/persistence/sync_aux_read_adapter.py`, and the contract map is `docs/reference/sync-protocol.md`. |
| Request stuck in processing | Start with `app/adapters/content/url_processor.py`, `app/adapters/content/platform_extraction/lifecycle.py`, `app/db/models/core.py::RequestProcessingJob`, and `docs/reference/troubleshooting.md#request-stuck-in-processing`; keep correlation IDs intact across any repair. |
| LLM parse failure | Parse/repair lives in `app/adapters/content/llm_response_workflow_attempts.py`, `app/adapters/content/llm_response_workflow_repair.py`, and `app/core/summary_contract.py`; runtime prompt/schema binding is `SummaryContractDescriptor` plus `PromptManager.get_contract_system_prompt()`; LangGraph retry topology is `app/agents/langgraph/graph.py`; failure recipe is `docs/reference/troubleshooting.md#json-parsing-failures`. |
| Extraction provider behavior | Generic URL extraction is `app/adapters/content/scraper/` plus `app/adapters/content/platform_extraction/`; platform-specific bypasses are `app/adapters/youtube/`, `app/adapters/twitter/`, and `app/adapters/academic/`; provider docs are `docs/explanation/scraper-chain.md`. |
| Source ingestion and vector repair | Source ingestors live in `app/adapters/ingestors/`, RSS/digest helpers in `app/adapters/rss/` and `app/adapters/digest/`, signal API in `app/api/routers/social/signals.py`, vector reconciliation in `app/infrastructure/vector/reconciliation.py`, `app/cli/reconcile_vector_index.py`, and `app/cli/backfill_vector_store.py`; new vectorized entity types should implement `VectorIndexedEntityAdapter`; vector drift docs are `docs/cocoindex.md`. |

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
|   +-- llm/            # Provider-agnostic LLM abstraction
|   +-- openrouter/     # OpenRouter client and helpers
|   +-- telegram/       # Bot logic, command_handlers/, access controller
|   +-- twitter/        # Twitter/X two-tier extractor (Firecrawl + Playwright)
|   +-- youtube/        # yt-dlp + transcript extraction
+-- agents/             # Classic agents + LangGraph summarize/validate/repair graph
+-- api/                # Mobile API (FastAPI, JWT, sync, collections, streams, digest, ...)
+-- application/        # DDD application layer (DTOs, use cases)
+-- config/             # Settings, scraper config, runtime tuning
+-- core/               # URL utils, JSON utils, summary contract descriptors/schema, lang detection
+-- db/                 # Models + Database session manager + Alembic migrations
+-- di/                 # Runtime composition
+-- domain/             # Domain models and services
+-- infrastructure/     # Persistence, cache, messaging, vector store, embedding, cocoindex
+-- mcp/                # Model Context Protocol server
+-- observability/      # Metrics, tracing
+-- prompts/            # LLM system prompts (en/ru, summary / combined_summary / instructor)
+-- security/           # Token crypto (Fernet), redaction
+-- tasks/              # Taskiq tasks (github_sync, reconcile_vector_index, digest, ...)
```

Skill, doc, and ops trees: `.claude/skills/` (project skills), `docs/` (explanation + reference), `ops/` (Docker / monitoring / config), `tools/scripts/` (dev utilities).

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

Task-oriented skills under `.claude/skills/`. Each carries its own workflow, trigger keywords, and dynamic context (live DB queries). Reach for these instead of re-deriving the steps.

| Skill | Use when |
|---|---|
| `adding-telegram-command` | adding a new `/foo` slash command (handler + registry wiring) |
| `alembic-migrations` | adding or modifying SQLAlchemy models / Postgres schema |
| `inspecting-database` | querying Postgres for requests, summaries, LLM calls, crawl results |
| `debugging-apis` | Firecrawl / OpenRouter request-response inspection, retry / cost / rate-limit triage |
| `validating-summaries` | summary JSON contract checks, character-limit failures |
| `langgraph-summarize-loop` | retry / repair-loop / `attempt_trigger` debugging |
| `vector-index-sync` | Qdrant + `summary_embeddings` + CocoIndex reconciliation |
| `scraper-chain-debugging` | content-scraper fallback chain failures |
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
- **Linting:** ruff (see `pyproject.toml`); B006 and B023 enforced (see Operating Rules).
- **Types:** mypy, `python_version = "3.13"`.
- **Testing:** pytest + pytest-asyncio, hypothesis, pytest-benchmark. Use `tests/db_helpers_async.py` (`create_request`, `insert_summary`, ...) instead of writing fresh fixtures or calling ORM models directly. E2E tests gated by `E2E=1`.
- **CI** (`.github/workflows/ci.yml`): lockfile freshness, lint+format+type, unit tests with 80% coverage, OpenAPI validation, radon complexity, security (Bandit, pip-audit, Safety, Gitleaks), frontend (`web-build`, `web-test`, `web-static-check`), Docker image build. Optional GHCR publish on `PUBLISH_DOCKER=true`.

## Key File References

| File | Role |
|---|---|
| `bot.py` | Entrypoint -- wires everything |
| `app/adapters/telegram/message_router.py` | Central routing |
| `app/adapters/content/url_processor.py` | URL processing orchestration |
| `app/adapters/content/scraper/` | Scraper protocol, chain, factory, providers |
| `app/core/summary_contract.py` | Summary contract descriptors and strict validation |
| `app/core/summary_schema.py` | Pydantic model for the contract |
| `app/core/url_utils.py` | URL normalization + `dedupe_hash` |
| `app/db/session.py` | `Database` -- sole DB entry point |
| `app/db/models/` | SQLAlchemy 2.0 typed models, grouped by area |
| `app/config/settings.py` | Configuration loading |
| `app/agents/langgraph/graph.py` | Summarize/validate/repair retry graph |
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

`LLMCall` rows carry `attempt_index` (1-based, monotonic per `request_id`) and `attempt_trigger` (Postgres enum: `initial`, `user_retry`, `auto_backfill`, `repair_loop`, `stream_fallback_retry`) so retries and the LangGraph repair loop are queryable without timestamp inference.

Schema and migration workflow: `alembic-migrations` skill + `docs/reference/data-model.md`.

## Environment Variables

Full reference (820 lines): `docs/reference/environment-variables.md`. Load-bearing ones:

| Var | Purpose |
|---|---|
| `ALLOWED_USER_IDS` | Comma-separated Telegram user IDs allowed to use the bot |
| `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, `OPENROUTER_FALLBACK_MODELS` | LLM provider and cascade |
| `LLM_CALL_TIMEOUT_SEC`, `LLM_PER_MODEL_TIMEOUT_MIN_SEC`, `LLM_PER_MODEL_TIMEOUT_OVERRIDES` | LLM cascade budget shaping |
| `DATABASE_URL` | Postgres DSN |
| `DIGEST_ENABLED`, `API_BASE_URL` | Channel digest subsystem on/off + Mini App callback URL |
| `GITHUB_TOKEN_ENCRYPTION_KEY` | Fernet key for at-rest GitHub PAT / OAuth tokens |
| `EMBEDDING_PROVIDER` | `local` (sentence-transformers) or `gemini` -- switching invalidates all existing vectors |
| `RATATOSKR_COCOINDEX_ENABLED`, `VECTOR_RECONCILE_ENABLED` | Vector-index sync writers |
| `FIELDTHEORY_SYNC_ENABLED`, `FIELDTHEORY_SYNC_CRON`, `FIELDTHEORY_WIKI_SYNC_CRON` | Master switch + cron for the two fieldtheory delta-scan Taskiq jobs (bookmark + wiki). Both jobs share the `enabled` flag. |
| `FIELDTHEORY_BOOKMARKS_DB_PATH`, `FIELDTHEORY_LIBRARY_PATH`, `FIELDTHEORY_IDEAS_PATH` | Container-side paths to the host-mounted `~/.fieldtheory/` subtrees (`bookmarks.db`, `library/`, `ideas/`). Defaults: `/fieldtheory/...` — bind-mounted read-only by the operator. |

## Task Board

Tasks live as Obsidian Tasks-compatible Markdown lines inside per-task notes. Source of truth: `docs/tasks/issues/<slug>.md`. The `repo-task-board` skill carries the full workflow (templates, transitions, lifecycle); only the canonical layout is repeated here:

- `docs/tasks/issues/<slug>.md` -- one note per task (YAML frontmatter + canonical `- [ ]` line + spec). **Delete the file on close** -- git history is the audit trail.
- `docs/tasks/active.md`, `backlog.md`, `blocked.md`, `dashboard.md` -- Obsidian Tasks query views, NOT task storage. Do not add task lines to these files.
- `docs/tasks/board.md` -- Kanban visualization.

When implementing a task, also update any CLAUDE.md or skill content that the change makes stale, and commit both together.

---

**Last Updated:** 2026-05-23

Reading order for orientation: this file → `docs/SPEC.md` → relevant `docs/explanation/*.md` or `docs/reference/*.md` → matching `.claude/skills/<name>/SKILL.md`.
