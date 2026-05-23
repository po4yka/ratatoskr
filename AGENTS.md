# AGENTS.md -- AI Agent Guide for Ratatoskr

This document provides project context for AI coding agents (Codex, Copilot, etc.). For the full guide see `CLAUDE.md`.

## Workspace skills

Cross-repo skills (`openapi-bump-cross-repo`, `local-stack-up`, `frost-token-mirror`, `workspace-status`) live in the parent workspace at `../.claude/skills/`. To pick them up when working inside this repo, launch Claude with `claude --add-dir ..`. See `../AGENTS.md` for the workspace overview and the cross-repo OpenAPI contract.

## Project Overview

Async Telegram bot that summarizes web articles, YouTube videos, and forwarded channel posts. Returns structured JSON summaries with a strict contract. Single Docker container, owner-only access.

**Stack:** Python 3.13+, Telethon, Scrapling/Firecrawl/Playwright (scraper chain), OpenRouter (LLM), LangChain/LangGraph (structured output + retry graph), PostgreSQL 16 via SQLAlchemy 2.0 + asyncpg (Alembic migrations), Qdrant (vector store), CocoIndex (optional live vector sync), Taskiq (Redis-backed worker), FastAPI, React 18 + TypeScript + Vite (Frost web frontend).

## Architecture

```
Telegram/API -> MessageRouter -> URL/Forward Handler -> ScraperChain -> LangGraph/LLM -> Summary JSON -> PostgreSQL + Qdrant
```

### Key Layers

| Layer | Location | Purpose |
|-------|----------|---------|
| Telegram | `app/adapters/telegram/` | Bot orchestration, routing, commands |
| Content | `app/adapters/content/` | Scraper chain (Scrapling -> Defuddle -> Firecrawl -> Playwright -> Crawlee -> direct HTTP) |
| YouTube | `app/adapters/youtube/` | yt-dlp download, transcript extraction |
| Twitter/X | `app/adapters/twitter/` | Firecrawl + Playwright extraction |
| GitHub | `app/adapters/github/`, `app/tasks/github_sync.py`, `app/api/routers/repositories.py`, `app/api/routers/auth/github.py` | GitHub repo ingestion, daily stars sync (cron `0 2 * * *` UTC), LangChain structured-output repo analysis, semantic search via `repository_embeddings` + Qdrant. Tokens encrypted at rest with Fernet (`cryptography`). See `docs/explanation/github-repository-ingestion.md`. |
| LLM | `app/adapters/llm/`, `app/adapters/openrouter/` | Provider-agnostic LLM interface; summary workflow uses the `SummaryContractDescriptor` default contract bundle for schema/prompt/repair response formats |
| Agents | `app/agents/`, `app/agents/langgraph/` | Classic agent wrappers plus LangGraph summarize/validate retry graph and checkpointing |
| Domain | `app/domain/` | Business models and domain services |
| Application | `app/application/` | DTOs, ports, use cases, and application services |
| Infrastructure | `app/infrastructure/` | Concrete persistence, vector search, cache, and messaging adapters |
| DI | `app/di/` | Runtime composition only |
| Core | `app/core/` | URL normalization, JSON parsing, summary contract, logging |
| Database | `app/db/` | SQLAlchemy 2.0 typed declarative models in `models/` (split by area), `Database` (`session.py`) is sole DB entry point, Alembic migrations in `alembic/versions/` |
| API | `app/api/` | FastAPI REST API with JWT auth |
| Search | `app/application/services/`, `app/infrastructure/search/`, `app/infrastructure/embedding/`, `app/infrastructure/cocoindex/` | Search workflows, vector search, embedding services, optional CocoIndex live sync, and vector reconciliation adapters |
| MCP | `app/mcp/` | Model Context Protocol server |

## Critical Files

- `app/adapters/telegram/message_router.py` -- Central routing logic
- `app/adapters/content/url_processor.py` -- URL processing orchestration
- `app/core/summary_contract.py` -- Summary descriptor registry and strict contract validation
- `app/core/url_utils.py` -- URL normalization and deduplication
- `app/agents/langgraph/graph.py` -- LangGraph summarize/validate retry graph
- `app/infrastructure/cocoindex/flow.py` -- CocoIndex summary + repository Qdrant flows
- `app/infrastructure/vector/point_ids.py` -- Shared deterministic Qdrant point IDs
- `app/db/models/` -- Database schema (SQLAlchemy 2.0 typed declarative models, grouped by area)
- `app/db/session.py` -- `Database` async-session facade (sole DB entry point)
- `app/config/settings.py` -- Configuration loading
- `app/config/scraper.py` -- Scraper chain configuration
- `bot.py` -- Entrypoint
- `docs/SPEC.md` -- Full technical specification (canonical reference)

## Agent Implementation Map

| Need | Start here | Contract / failure doc |
|------|------------|------------------------|
| Auth and sessions | `app/api/routers/auth/`, `app/api/routers/auth/tokens.py`, `app/api/routers/auth/cookies.py`, `app/infrastructure/persistence/repositories/auth_repository.py`, `app/db/models/core.py::RefreshToken` | `docs/reference/mobile-api.md#authentication-modes`, `docs/reference/troubleshooting.md#refresh-token-stops-working` |
| API contracts | `app/api/main.py`, `app/api/models/`, `app/api/routers/`, `tools/scripts/generate_openapi.py`, `docs/openapi/mobile_api.yaml` | `docs/reference/openapi-contract-workflow.md`, `docs/reference/mobile-api.md#api-surface-freeze-policy` |
| Sync v2 | `app/api/routers/sync.py`, `app/api/services/sync/`, `app/infrastructure/persistence/sync_aux_read_adapter.py` | `docs/reference/sync-protocol.md`, `docs/reference/troubleshooting.md#sync-conflicts` |
| Request processing stuck | `app/adapters/content/url_processor.py`, `app/adapters/content/platform_extraction/lifecycle.py`, `app/db/models/core.py::RequestProcessingJob` | `docs/reference/troubleshooting.md#request-stuck-in-processing` |
| LLM parse / repair | `app/adapters/content/llm_response_workflow_attempts.py`, `app/adapters/content/llm_response_workflow_repair.py`, `app/core/summary_contract.py`, `app/prompts/manager.py`, `app/agents/langgraph/graph.py` | `docs/reference/troubleshooting.md#json-parsing-failures`, `docs/reference/summary-contract.md` |
| Extraction providers | `app/adapters/content/scraper/`, `app/adapters/content/platform_extraction/`, `app/adapters/youtube/`, `app/adapters/twitter/`, `app/adapters/academic/` | `docs/explanation/scraper-chain.md`, `docs/reference/troubleshooting.md#content-extraction-failures` |
| Source ingestion and signals | `app/adapters/ingestors/`, `app/adapters/rss/`, `app/adapters/digest/`, `app/api/routers/social/signals.py` | `docs/guides/configure-source-ingestors.md` |
| Vector drift / reconciliation | `app/infrastructure/vector/reconciliation.py`, `app/cli/reconcile_vector_index.py`, `app/cli/backfill_vector_store.py`, `app/infrastructure/cocoindex/flow.py` | `docs/cocoindex.md`, `docs/reference/troubleshooting.md`; extend via `VectorIndexedEntityAdapter` |

Generated API artifacts live in `docs/openapi/mobile_api.yaml` and `docs/openapi/mobile_api.json`; do not edit them manually. Change routers/models first, then run `make generate-openapi`, `make check-openapi-drift`, `make check-openapi-validate`, and `make check-openapi`.

## Development Commands

```bash
source .venv/bin/activate
make format          # ruff format + isort
make lint            # ruff
make type            # mypy
python -m app.cli.summary --url <URL>           # CLI test runner

# Pi deployment: build linux/arm64 image on the Mac, stream to the Pi over
# SSH, restart via compose. The Pi never runs `docker build`. Requires
# `ssh raspi`, expects repo at `~/ratatoskr` on the Pi (override with
# RASPI_REMOTE_PATH). Pass SERVICE=mobile-api to ship the API image instead.
make pi-deploy                                  # build + ship + restart
make pi-deploy SERVICE=mobile-api
bash tools/scripts/build-and-deploy-pi.sh --help
```

## Code Conventions

- **Formatting:** ruff format + isort (profile=black)
- **Linting:** Ruff (see `pyproject.toml`)
- **Type checking:** mypy (`python_version = "3.13"`)
- **Pre-commit hooks:** ruff -> isort -> mypy
- **Testing:** pytest + pytest-asyncio. Test DB helpers in `tests/db_helpers.py`
- **Commits:** Conventional Commits (`feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`)

## Key Rules

1. All URLs must be normalized before deduplication (`app/core/url_utils.py`)
2. All user-visible errors must include `Error ID: <correlation_id>`
3. Persist everything: scraper responses, LLM calls, Telegram messages
4. Always redact `Authorization` headers before logging
5. Update both `en/` and `ru/` prompts when changing LLM behavior
6. Validate summary JSON with `app/core/summary_contract.py`
7. Database changes require migration via `app/cli/migrate_db.py` + docs/SPEC.md update
8. State scope explicitly when giving an instruction; don't expect silent generalization across items
9. Tell the agent what to do, not what to avoid (e.g., "use `tests/db_helpers.py`" vs. "don't write new fixtures")
10. Front-load the full task spec on the first turn; iterative refinement loses context against multi-step plans
11. Make independent tool calls in parallel; sequence only when one result determines the next call's parameters
12. Read code before asserting its behavior; cite `file:line` for non-obvious claims

## Database

PostgreSQL via SQLAlchemy 2.0 + asyncpg. Typed declarative models live under `app/db/models/` (split by area: `core.py`, `aggregation.py`, `batch.py`, `collections.py`, `digest.py`, `repository.py`, `rss.py`, `rules.py`, `signal.py`, `topic_search.py`, `user_content.py`) and are aggregated through `app/db/models/__init__.py::ALL_MODELS`. `Database` (`app/db/session.py`) is the sole DB entry point and exposes async sessions/transactions; full-text search runs on a Postgres `TSVECTOR` + GIN column. Schema migrations are managed by Alembic (`app/db/alembic/versions/`) and applied with `python -m app.cli.migrate_db`.

## Summary JSON Contract

Defined in `app/core/summary_contract.py` (descriptor registry and validation) and `app/core/summary_schema.py` (Pydantic model). Core fields: `summary_250`, `summary_1000`, `tldr`, `key_ideas`, `topic_tags`, `entities`, `estimated_reading_time_min`. The current `default` descriptor pairs the provider schema name, EN/RU prompt loader, JSON response formats, and compatibility mapper; use it instead of hand-assembling schema/prompt kwargs in generic workflows. Full contract has 35+ fields. See `docs/SPEC.md` and `docs/reference/summary-contract.md`.

---

## Task Board

This repository uses Obsidian Tasks-compatible Markdown checkboxes as the canonical task system.

Before changing task-related files, use the `repo-task-board` skill if available.

**Source of truth:** `docs/tasks/issues/<slug>.md` — one note per task (kebab-case title) with YAML frontmatter.

**Query views** (do not add task lines here): `docs/tasks/active.md` · `docs/tasks/backlog.md` · `docs/tasks/blocked.md` · `docs/tasks/dashboard.md`

Canonical syntax (lives inside `issues/<slug>.md`):

```md
- [ ] #task <imperative title> #repo/ratatoskr #area/<area> #status/<status> <priority>
```

Allowed statuses: `#status/backlog` · `#status/todo` · `#status/doing` · `#status/review` · `#status/blocked` · `#status/done` · `#status/dropped`

Rules: one `- [ ]` line per per-task note · update `status:` frontmatter AND `#status/*` tag together · add `#blocked` + indented reason + `blocked_by:` frontmatter when blocking · delete the `issues/<slug>.md` file when done (git history is the audit trail) · never add task lines to the query view files.
