# Ratatoskr technical specification

Ratatoskr is an async Telegram and HTTP service that ingests web pages, videos, forwarded posts, repositories, feeds, and related sources; produces structured summaries; and stores the resulting artifacts in PostgreSQL. Generic URL summaries run through a deterministic LangGraph workflow. LLM calls use the configured OpenRouter, OpenAI, Anthropic, or Ollama adapter.

This page is the stable specification index. Detailed facts live in focused references; executable contracts live in code, SQLAlchemy models, Alembic migrations, and generated OpenAPI.

## Architecture

The summary path is:

```text
Telegram or API
  → GraphURLProcessor
  → ingest → extract → ground → build_prompt → summarize
  → validate ↺ repair → enrich → persist → notify
  → PostgreSQL + Qdrant + user response
```

Platform-specific extractors handle YouTube, Twitter/X, and academic papers. Other URLs reach the ordered 13-provider scraper chain. Taskiq and Redis run scheduled or asynchronous work such as digests, source ingestion, GitHub synchronization, backups, and vector reconciliation.

See [Architecture Overview](explanation/architecture-overview.md), [Graph and Agent Architecture](explanation/multi-agent-architecture.md), and [Scraper Chain](explanation/scraper-chain.md).

## Data Model

PostgreSQL is the relational source of truth. SQLAlchemy 2.0 models live in `app/db/models/`, are registered through `ALL_MODELS`, and are changed through Alembic revisions in `app/db/alembic/versions/`. `app/db/session.py::Database` is the application entry point for async sessions and transactions.

The schema covers request processing and artifacts, users and authentication, collections and user content, digests/RSS/signals, repositories and git mirrors, exports and automation, transcription, browser sessions, task failures, and derived embedding metadata.

See [Data Model Reference](reference/data-model.md).

## Database Schema

Field definitions, foreign keys, enums, indexes, and constraints are authoritative in the owning model plus its Alembic revision. The maintained table catalog and core ER diagram are in [Data Model Reference](reference/data-model.md). Telethon session SQLite files and the read-only external X bookmarks SQLite database are integration stores, not Ratatoskr's relational schema.

## Summary JSON Contract

Every normal summary is shaped and validated through the descriptor registry in `app/core/summary_contract.py` and the Pydantic schema in `app/core/summary_schema.py`. The default descriptor binds provider response format, EN/RU prompt selection, validation, repair, and compatibility shaping.

See [Summary Contract Reference](reference/summary-contract.md) and [Contract Design](explanation/summary-contract-design.md).

## Summary JSON Contract (Canonical)

The reference above defines the public fields, size bounds, enums, array limits, and compatibility rules. Update both EN and RU prompt variants when changing LLM behavior, and validate output through the descriptor rather than duplicating schema arguments in a caller.

## Mobile REST API

FastAPI routers and Pydantic models are the editable source. The generated machine-readable contracts are:

- `docs/openapi/mobile_api.yaml`
- `docs/openapi/mobile_api.json`

Do not edit generated OpenAPI directly. Follow [OpenAPI Contract Workflow](reference/openapi-contract-workflow.md) and use [Mobile API](reference/mobile-api.md) for ownership, authentication, envelope, and streaming guidance.

## Search

Ratatoskr combines PostgreSQL full-text search with optional Qdrant vector or hybrid retrieval. PostgreSQL remains authoritative; Qdrant uses deterministic point IDs, synchronous fast-path writes for supported entities, and Taskiq reconciliation/backfill to repair drift.

See [Qdrant Setup](guides/setup-qdrant-vector-search.md) and [Vector Index Synchronization](vector-index-sync.md).

## Mixed-source aggregation

Aggregation sessions group multiple URLs, forwards, or attachments and persist per-source provenance before producing a combined result. The application agents in `app/agents/` provide focused multi-source extraction and aggregation behavior; they do not replace the URL summary graph.

## Channel digest

The digest subsystem uses a separate Telethon userbot session to read subscribed channels. Taskiq schedules analysis and delivery; preferences and delivery state live in PostgreSQL. See [Digest Subsystem Operations](reference/digest-subsystem-ops.md).

## Correlation IDs

A correlation ID connects ingress, request/job state, scraper attempts, LLM calls, logs, progress events, and user-visible failures. User-visible errors must include `Error ID: <correlation_id>`; code must preserve the ID across graph nodes, retries, tasks, and adapters.

## Authentication and authorization

Telegram access is allowlist-first. HTTP and MCP surfaces use the authentication modes declared by their routers and generated OpenAPI, combined with user/client allowlists and route-level ownership checks. Persisted user-owned records retain `user_id` filters as defense in depth.

See [Mobile API](reference/mobile-api.md), [MCP Server](reference/mcp-server.md), and [Secret Rotation](runbooks/secret-rotation.md).

## Deployment and operations

Production runs through `ops/docker/docker-compose.yml`. The base stack separates bot, API, worker, scheduler, migrations, PostgreSQL, Redis, Qdrant, and PostgreSQL backup roles; optional profiles add scraper sidecars, Webwright, monitoring, MCP, or cloud Ollama checks.

See [Production Deployment](guides/deploy-production.md), [Environment Variables](reference/environment-variables.md), [Backup and Restore](guides/backup-and-restore.md), and [Troubleshooting](reference/troubleshooting.md).

## External Systems & Authoritative Docs

| System | Local contract |
|---|---|
| Telegram / Telethon | Adapter code under `app/adapters/telegram/` and digest operations reference |
| LLM providers | `app/adapters/llm/protocol.py`, provider adapters, and [LLM Providers](reference/llm-providers.md) |
| Scraper sidecars | Provider adapters, compose services, and [Scraper Chain](explanation/scraper-chain.md) |
| GitHub | `app/adapters/github/`, repository routers/tasks, and [GitHub Ingestion](explanation/github-repository-ingestion.md) |
| Qdrant | Infrastructure vector adapters and [Vector Index Synchronization](vector-index-sync.md) |
| MCP | `app/mcp/` and [MCP Server](reference/mcp-server.md) |
| Web frontend | FastAPI serving contract plus the external `ratatoskr-web` repository; see [Web Frontend](reference/frontend-web.md) |

## Change rules

- Normalize URLs before deduplication.
- Persist scraper attempts, LLM calls, messages, and summary outcomes with correlation context.
- Redact authorization and secret material before logging or persistence.
- Update both language prompt variants for LLM behavior changes.
- Pair schema changes with Alembic migrations and documentation updates.
- Pair API changes with regenerated and validated OpenAPI artifacts.

Last audited: 2026-07-15.
