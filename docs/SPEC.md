# Ratatoskr — Technical Specification

> This page is a navigation index. All substantive content lives in the linked canonical pages.

Async Telegram bot: accepts web article URLs, YouTube videos, and forwarded channel posts; summarizes via a multi-provider scraper chain and the OpenRouter LLM adapter; persists relational artifacts in PostgreSQL. Upstream model families are selected through OpenRouter model IDs such as `openai/...` or `anthropic/...`, not separate runtime providers. The project also exposes a FastAPI mobile API, a React web frontend, and an MCP server for external AI agents.

---

## Architecture

Component diagram, request lifecycle, layered (hexagonal) view, goals, non-goals, user/access-control model, and the canonical subsystem index.

→ [Architecture Overview](explanation/architecture-overview.md)

---

## Data Model

Complete PostgreSQL schema reference: all core tables (users, requests, crawl_results, llm_calls, summaries, aggregation sessions, signal-scoring tables, social connection credential/status tables), indexes, relationships, ER diagram, common queries, database maintenance, migrations, mixed-source aggregation model, and URL normalization/deduplication rules.

→ [Data Model Reference](reference/data-model.md)

Outbound email delivery is optional and off by default. Alembic 0040 adds `user_email_addresses` for one-time verification tokens and `email_deliveries` for provider success/failure logging; `user_digest_preferences.delivery_channel` selects `telegram` or a verified email address for scheduled/on-demand digest delivery.

Passwordless and social identity providers are linked to existing users rather than creating public accounts. Alembic 0041 adds `user_identities` for provider/subject/email mappings and `magic_link_tokens` for hashed one-time email login tokens; Apple Sign-In and magic-link login reuse the existing JWT access/refresh envelope.

Telethon session files are the only intentional SQLite carve-out. They are client session stores owned by Telethon, validated by `app/adapters/digest/session_validator.py`, and are not part of Ratatoskr's relational store or PostgreSQL migration.

---

## Summary JSON Contract

Strict JSON schema enforced on every summary: field definitions, character limits, array lengths, enums, validation rules, self-correction loop, backward-compatibility policy, and a complete example. Runtime integration goes through `SummaryContractDescriptor` in `app/core/summary_contract.py`; the current `default` descriptor pairs the `summary_schema` provider response format, EN/RU system prompts loaded by `PromptManager`, and `validate_and_shape_summary()` as the compatibility mapper so future contract variants can be added without changing the default wire shape.

→ [Summary Contract Reference](reference/summary-contract.md) · [Contract Design](explanation/summary-contract-design.md)

---

## Mobile REST API

Full endpoint index, envelope/error contract, authentication modes (Telegram login, secret-key flow, JWT refresh), aggregations surface, signal scoring surface, sync model, search parameters, collections, digest, system-maintenance endpoints, and the real-time `GET /v1/requests/{id}/stream` SSE endpoint that emits `phase` / `section` / `done` / `error` events for in-flight summaries.

→ [Mobile API Reference](reference/mobile-api.md)

Machine-readable contract: `docs/openapi/mobile_api.yaml` / `docs/openapi/mobile_api.json`

---

## API Contracts and Error Codes

External service API shapes (Firecrawl, OpenRouter, Telethon, yt-dlp) with request/response examples, rate limits, and error handling; plus the full internal error-code catalog (AUTH, VAL, EXT, LLM, YT, DB, SYNC, RATE, SYS, REDIS, VECTOR families).

→ [External API Contracts](reference/api-contracts.md) · [Error Codes Reference](reference/api-error-codes.md)

---

## Scraper Chain

Ten-provider ordered fallback chain (Scrapling → Crawl4AI → Firecrawl self-hosted → Defuddle → CloakBrowser → Playwright → Crawlee → direct HTML → ScrapeGraphAI → Webwright): provider taxonomy, deployment topology, quality gates, anti-fingerprinting, and configuration recipes. The final rung (`webwright`) is an LLM-driven Playwright browser-agent — ~10-30× the cost of a normal scrape, double-gated by `WEBWRIGHT_ENABLED` and a non-empty `WEBWRIGHT_HOST_ALLOWLIST`.

→ [Scraper Chain Explanation](explanation/scraper-chain.md) · [Webwright Integration](explanation/webwright.md)

---

## Academic-Paper Handling

Scholarly-paper URLs (arXiv, SSRN, NBER, OSF preprints, ResearchGate, RePEc) are routed through a dedicated platform extractor (`app/adapters/academic/`) instead of the generic scraper chain. The flow:

1. **URL detection** — `parse_academic_paper_url(url)` recognizes host + canonical paper id (e.g. `arxiv:2301.00001`, `ssrn:6531478`).
2. **Landing HTML** — fetched via the scraper chain so patchright stealth handles Cloudflare-gated hosts (SSRN, ResearchGate). Title + abstract are extracted from the resulting markdown.
3. **PDF resolution** — per-host URL rewriters yield the canonical PDF URL without a network round-trip (`pdf_url_for(ref)`); ResearchGate and RePEc fall back to anchor discovery in the landing markdown.
4. **PDF extraction** — downloaded via httpx (60 s timeout, redirects followed), text extracted via pymupdf on a background thread (no Pillow / image rendering — academic flow feeds a text LLM).
5. **LLM input** — composed as `# Title / ## Abstract / ## Body`; abstract is always first so the chunker can preserve the author-authored TL;DR even when the body is truncated. Prompt sections (EN + RU) instruct the model to set `source_type='research'` and structure `key_ideas` around claims, evidence, methods, and limitations.
6. **Paywall fallback** — paywall / 403 / 404 / network failure on the PDF leg degrades to abstract-only with an explicit `[PDF unavailable: <reason>]` marker in `content_text`. Never a hard `Content Extraction Failed`.

**Dedupe** — `requests.paper_canonical_id` (added in Alembic 0012) stores the canonical id; URL shapes pointing at the same paper (`/abs/X` vs `/pdf/X.pdf`, `v1` vs `v2`, `papers.cfm` vs `Delivery.cfm`) collapse to one `requests` row per user via the `(user_id, paper_canonical_id)` partial unique index added in Alembic 0038. `SourceKind.ACADEMIC_PAPER` is the system-level discriminator surfaced to the mobile API.

---

## Multi-Agent Architecture

ContentExtraction, Validation, and WebSearch agents; self-correction retry loop is the LangGraph summarize graph's `validate ↺ repair` cycle (`app/application/graphs/summarize/nodes/validate.py` + `repair.py`), backed by `app/application/services/summarization/graph_llm.py::summarize_with_instructor`; signal-scoring v0 integration; usage examples and test hints.

→ [Multi-Agent Architecture](explanation/multi-agent-architecture.md)

---

## Environment Variables and Configuration

Complete reference for all environment variables grouped by subsystem, plus YAML config file reference.

→ [Environment Variables Reference](reference/environment-variables.md) · [YAML Config Reference](reference/config-file.md)

---

## Deployment and Operations

Production deployment guide, Docker Compose profiles, volume mounts, and channel-digest subsystem ops.

→ [Production Deployment Guide](guides/deploy-production.md) · [Digest Subsystem Ops](reference/digest-subsystem-ops.md) · [Secret Rotation Runbook](runbooks/secret-rotation.md) · [Pi SQLite→Postgres Cutover Runbook](runbooks/pi-postgres-cutover.md)

---

## Dependency Supply Chain

Private Safety CLI index topology, `SAFETY_API_KEY` dependency, `UV_INDEX_STRATEGY: unsafe-best-match` and PyTorch CPU extra-index resolution surface, the lapse failure mode (PyPI fallback re-resolving a yanked release), and the layered defenses: `!=` exclusions in `pyproject.toml`, the `check_excluded_versions.py` requirements-file guard, the Safety-index reachability step in lock-regeneration workflows, and the lockfile-freshness CI backstop.

→ [Dependency Supply-Chain Reference](reference/dependency-supply-chain.md)

---

## Troubleshooting and FAQ

Common failure modes, debugging workflow, external API error resolution, and frequently asked questions.

→ [Troubleshooting Reference](reference/troubleshooting.md) · [FAQ](explanation/faq.md)

---

## Web Frontend

React SPA serving contract, routes, hybrid auth modes, and local development workflow.

→ [Web Frontend Reference](reference/frontend-web.md)

---

## Observability

Prometheus metrics, structured logs, correlation-ID tracing, owner-safe diagnostics, and the Loki/Promtail/Grafana monitoring stack. Owner diagnostics for `/v1/admin/diagnostics` are composed by `DiagnosticsService`, which gathers health checks, scraper configuration, vector lag, queue backlog, storage growth, integration failures, social provider connection summaries, and redacted provider status behind a short process-local cache. Connected social auth/content paths additionally expose provider fetch, token-refresh, rate-limit, and connection-status counters while keeping fetch-attempt metadata sanitized. Taskiq workers use opt-in retry labels with a dead-letter table (`taskiq_failed_jobs`) for terminal background-job failures and expose `ratatoskr_taskiq_retries_total{task,outcome}` for retry, dead-letter, and success-after-retry monitoring.

→ [Observability Strategy](explanation/observability-strategy.md)

---

## On-Disk Git Mirroring

Full bare-clone backup of repository history, refs, objects, and packs via `git clone --mirror`. Complements the API-only GitHub metadata ingestion by preserving complete git history for GitHub-linked repos (starred/owned via the GitHub integration) and arbitrary git URLs. Covers the ported gitout engine modules (error classification, adaptive retry, storage circuit breaker, post-sync maintenance, LFS support, README extraction), the `git_mirrors` table schema, credential handling (Fernet-encrypted GitHub tokens injected without logging), the `ratatoskr.git_backup.sync` Taskiq job, Telegram `/mirror` + `/mirrors` commands, and the `/v1/git-mirrors` REST surface.

→ [Git Mirroring Explanation](explanation/git-mirroring.md)

---

## GitHub Repository Schema

Three tables added by the GitHub repository ingestion subsystem (`app/db/models/repository.py`). They have no foreign key to `summaries`; repos use the `RepoAnalysis` contract, not the 35-field `Summary` contract.

### `repositories`

| Column | Type | Null | Purpose |
|--------|------|------|---------|
| `id` | integer PK | no | Auto-increment surrogate key |
| `github_id` | bigint | no | GitHub's stable numeric repo ID |
| `owner` | varchar(100) | no | Owner login |
| `name` | varchar(200) | no | Repo name |
| `full_name` | varchar(320) | no | `owner/name` |
| `url` | varchar(500) | no | Canonical `https://github.com/owner/repo` URL |
| `homepage_url` | varchar(500) | yes | Project homepage |
| `description` | text | yes | GitHub description |
| `primary_language` | varchar(100) | yes | Dominant language |
| `languages_json` | jsonb | yes | Full language breakdown: `{"Python": 12345}` |
| `topics_json` | jsonb | yes | Topic list: `["web", "async"]` |
| `stars` | integer | no | Star count; refreshed every sync |
| `forks` | integer | no | Fork count; refreshed every sync |
| `watchers` | integer | no | Watcher count; refreshed every sync |
| `default_branch` | varchar(100) | yes | Default branch for README fetch |
| `license_spdx` | varchar(100) | yes | SPDX license identifier |
| `is_archived` | boolean | no | Archived on GitHub |
| `is_fork` | boolean | no | Fork of another repo |
| `is_template` | boolean | no | Template repo |
| `pushed_at` | timestamptz | yes | Last push time |
| `created_at_github` | timestamptz | yes | Repo creation time on GitHub |
| `readme_excerpt` | text | yes | First `GITHUB_README_MAX_BYTES` of raw README |
| `readme_etag` | varchar(200) | yes | HTTP ETag; reserved for conditional fetch |
| `analysis_json` | jsonb | yes | LLM-derived `RepoAnalysis` fields |
| `analysis_model` | varchar(200) | yes | Model used; surfaced for re-analyze affordance |
| `analysis_at` | timestamptz | yes | When analysis was last computed |
| `content_hash` | varchar(64) | yes | SHA256 of `description + sorted(topics) + readme_excerpt` |
| `source` | repo_source enum | no | `manual` or `starred` |
| `is_starred` | boolean | no | Currently in user's GitHub stars |
| `user_id` | bigint FK | no | References `users.telegram_user_id` |
| `last_synced_at` | timestamptz | no | Last metadata pull from GitHub |
| `pending_analysis` | boolean | no | LLM analysis deferred by budget cap |
| `created_at` | timestamptz | no | Row insertion time |
| `updated_at` | timestamptz | no | Last modification time |

Unique constraint: `(user_id, github_id)`. Indexes: `(user_id, is_starred)`, `(user_id, primary_language)`, `(user_id, pushed_at DESC)`, `(github_id)`.

Analyzed repository rows (`analysis_json IS NOT NULL`) are exported to Qdrant using the deterministic point UUID `uuid5(NAMESPACE_OID, f"{environment}:{user_scope}:repository:{repository_id}")` by the GitHub analysis fast path and backfilled by the Taskiq reconciler.

### `repository_embeddings`

| Column | Type | Null | Purpose |
|--------|------|------|---------|
| `id` | integer PK | no | Surrogate key |
| `repository_id` | integer FK unique | no | References `repositories.id` ON DELETE CASCADE |
| `model_name` | varchar(200) | no | Embedding model identifier |
| `model_version` | varchar(50) | no | Version; backfill CLI detects staleness on mismatch |
| `embedding_blob` | bytea | no | Serialized float32 embedding |
| `dimensions` | integer | no | Vector dimensionality |
| `language` | varchar(10) | yes | Language of embedded text |
| `content_hash` | varchar(64) | yes | SHA256 of repository text fed to the embedding model |
| `last_indexed_at` | timestamptz | yes | Last successful Qdrant point write |
| `index_status` | varchar(32) | no | `"pending"` until Qdrant write succeeds, then `"indexed"` |
| `created_at` | timestamptz | no | Row insertion time |

### `user_github_integrations`

| Column | Type | Null | Purpose |
|--------|------|------|---------|
| `id` | integer PK | no | Surrogate key |
| `user_id` | bigint FK unique | no | References `users.telegram_user_id` ON DELETE CASCADE |
| `auth_method` | github_auth_method enum | no | `pat` or `oauth_device` |
| `encrypted_token` | bytea | no | Fernet-encrypted access token |
| `token_scopes` | varchar(500) | yes | Scopes from GitHub token validation |
| `github_login` | varchar(100) | yes | Cached GitHub username |
| `github_user_id` | bigint | yes | GitHub's numeric user ID |
| `status` | github_integration_status enum | no | `active`, `needs_reauth`, or `revoked` |
| `last_synced_at` | timestamptz | yes | Most recent sync completion time |
| `last_sync_cursor` | varchar(500) | yes | Reserved for pagination cursor |
| `last_full_sync_at` | timestamptz | yes | Most recent full-pagination sync completion |
| `notified_needs_reauth_at` | timestamptz | yes | When the one-shot reauth DM was sent |
| `created_at` | timestamptz | no | Row insertion time |
| `updated_at` | timestamptz | no | Last modification time |

→ [GitHub Repository Ingestion](explanation/github-repository-ingestion.md) for data-flow, sync algorithm, and cost model.

---

*Last updated: 2026-05-23*
