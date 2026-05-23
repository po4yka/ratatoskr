# GitHub Repository Ingestion

How Ratatoskr indexes GitHub repositories as a first-class content source: manual URL ingestion, daily starred-repository sync, LLM-driven analysis, and semantic search over the resulting index.

**Audience:** Contributors extending the feature, operators configuring the sync, integrators querying the repository search API. **Type:** Explanation. **Related:** [`docs/SPEC.md`](../SPEC.md) (canonical contract), [`docs/explanation/architecture-overview.md`](architecture-overview.md) (subsystem index), [`docs/reference/environment-variables.md`](../reference/environment-variables.md) (full env-var table).

---

## Overview

The GitHub repository subsystem treats a GitHub repo the same way the rest of Ratatoskr treats an article or video: fetch structured metadata, pass it through an LLM analysis step, generate a vector embedding, and store the result in PostgreSQL and Qdrant for search. Two ingestion paths exist. The first is manual: the user pastes a `https://github.com/<owner>/<repo>` URL into Telegram, the web UI, or the CLI, and the `GitHubPlatformExtractor` fetches metadata and README in the same request, runs `AnalyzeRepositoryUseCase` inline, and embeds the output immediately. The use case depends on `RepositoryAnalysisRepositoryPort`; the SQLAlchemy adapter is `RepositoryAnalysisRepositoryAdapter`, so the analysis workflow can be tested without constructing a `Database` or ORM model. The agent prefers LangChain structured output through `LLMClient.chat_structured(RepoAnalysis)` and falls back to the legacy raw JSON path for adapters that do not support it. The second path is automated: a Taskiq cron job (`ratatoskr.github.sync_stars`, default `0 2 * * *`) paginates the authenticated user's `/user/starred` endpoint, upserts `Repository` rows, and runs analysis on any row whose content-hash has changed, subject to a configurable LLM budget cap. Both paths converge on the same storage schema and the same Qdrant collection. The repository embedding fast path writes search results immediately; the CocoIndex repository flow reconciles analyzed rows with the same deterministic Qdrant point IDs used by the live updater, so search results span manually ingested and auto-synced repos without distinction.

---

## Data Flow

### Manual ingestion path

```
github.com/<owner>/<repo> URL
  |
  v
URLProcessor (app/adapters/content/url_processor.py)
  |-- is_github_repo_url() check (app/adapters/github/url_patterns.py)
  v
GitHubPlatformExtractor (app/adapters/github/platform_extractor.py)
  |-- GitHubAPIClient.get_repo(owner, name)
  |-- GitHubAPIClient.get_readme(owner, name, default_branch)
  |   (GET /repos/{owner}/{repo}/readme, Accept: application/vnd.github.raw)
  |-- truncate README to GITHUB_README_MAX_BYTES (default 50 KB)
  |-- upsert Repository row  (source=manual, is_starred=false)
  v
analyze_repository use case (app/application/use_cases/analyze_repository.py)
  |-- reads/writes through RepositoryAnalysisRepositoryPort
  |-- infrastructure adapter: RepositoryAnalysisRepositoryAdapter
  v
RepoAnalysisAgent (app/agents/repo_analysis_agent.py)
  |-- compose user prompt from (description, topics, languages, readme_excerpt)
  |-- call LLM via chat_structured(RepoAnalysis) when supported
  |-- fall back to raw JSON LLM call for legacy adapters/tests
  |-- validate RepoAnalysis schema (app/core/repo_analysis_contract.py)
  |-- retry up to 3x with error feedback on schema failure
  |-- persist LLMCall row (attempt_index, attempt_trigger)
  v
RepoAnalysis JSON stored in repositories.analysis_json
  v
RepositoryEmbeddingGenerator (app/infrastructure/embedding/repository_embedding.py)
  |-- concatenate: purpose + tech_stack + architecture_summary + key_concepts
  |-- call EmbeddingFactory (local sentence-transformers or Gemini API)
  v
Qdrant upsert  (entity_type="repository", user_id=<id>)
RepositoryEmbedding row upserted (app/db/models/repository.py)
  |
  v
CocoIndex repository flow reconciles analyzed rows with same point IDs
  (app/infrastructure/cocoindex/flow.py, app/infrastructure/vector/point_ids.py)
```

### Stars sync path (Taskiq cron)

```
Taskiq scheduler (app/tasks/scheduler.py)
  |-- cron: GITHUB_SYNC_CRON (default "0 2 * * *")
  v
sync_all_active_integrations task (app/tasks/github_sync.py)
  |
  for each UserGitHubIntegration with status=active:
    |-- decrypt_token(integration.encrypted_token) -> plaintext PAT / OAuth token
    |-- GitHubAPIClient(token=...) constructed per-user
    |-- paginate GET /user/starred?sort=created&direction=desc&per_page=100
    |   (Accept: application/vnd.github.star+json  ->  starred_at timestamp)
    |-- early-exit when starred_at < integration.last_synced_at (incremental)
    |
    for each starred repo:
      |-- upsert Repository (source=starred, is_starred=true)
      |-- compute content_hash; if changed -> enqueue analyze_repository
      |-- if llm_calls_made >= GITHUB_LLM_DAILY_BUDGET:
      |     set pending_analysis=true, skip LLM
    |
    |-- flip is_starred=false for repos no longer in the listing (soft-unstar)
    |-- update integration.last_synced_at
    |
    on GitHubAuthError:
      |-- integration.status = needs_reauth
      |-- send one-shot Telegram DM (throttled via notified_needs_reauth_at)
    on GitHubRateLimitError:
      |-- suspend this user's sync until X-RateLimit-Reset epoch
      |-- continue with next user
```

---

## Schema

Three new tables live in `app/db/models/repository.py`. They have no foreign key to the `summaries` table by design: repos use a repo-shaped LLM analysis schema, not the 35-field `Summary` contract.

### `repositories`

| Column | Type | Nullable | Purpose |
|--------|------|----------|---------|
| `id` | integer PK | no | Auto-increment surrogate key |
| `github_id` | bigint | no | GitHub's stable numeric repo ID; unique across the table |
| `owner` | varchar(100) | no | Repository owner login |
| `name` | varchar(200) | no | Repository name |
| `full_name` | varchar(320) | no | `owner/name` composite; used in log context and display |
| `url` | varchar(500) | no | Canonical `https://github.com/owner/repo` URL |
| `homepage_url` | varchar(500) | yes | Project homepage if set |
| `description` | text | yes | GitHub repo description |
| `primary_language` | varchar(100) | yes | Dominant language as reported by GitHub |
| `languages_json` | jsonb | yes | Full language breakdown: `{"Python": 12345, "Rust": 678}` |
| `topics_json` | jsonb | yes | Repo topics list: `["web", "async"]` |
| `stars` | integer | no | Star count; refreshed every sync |
| `forks` | integer | no | Fork count; refreshed every sync |
| `watchers` | integer | no | Watcher count; refreshed every sync |
| `default_branch` | varchar(100) | yes | Default branch name; used for README fetch |
| `license_spdx` | varchar(100) | yes | SPDX license identifier (e.g., `MIT`, `Apache-2.0`) |
| `is_archived` | boolean | no | Whether GitHub has archived the repo |
| `is_fork` | boolean | no | Whether this is a fork of another repo |
| `is_template` | boolean | no | Whether this repo is a template |
| `pushed_at` | timestamptz | yes | Last push time from GitHub; used as an activity signal |
| `created_at_github` | timestamptz | yes | Repo creation time on GitHub |
| `readme_excerpt` | text | yes | First `GITHUB_README_MAX_BYTES` (50 KB default) of raw README |
| `readme_etag` | varchar(200) | yes | HTTP ETag from the README response; reserved for future conditional fetch |
| `analysis_json` | jsonb | yes | LLM-derived `RepoAnalysis`: `purpose`, `tech_stack`, `architecture_summary`, `key_concepts`, `code_patterns`, `use_cases`, `target_audience`, `maturity`, `key_dependencies`, `hallucination_risk`, `confidence` |
| `analysis_model` | varchar(200) | yes | Model string used to produce `analysis_json`; surface to frontend for re-analyze affordance |
| `analysis_at` | timestamptz | yes | When analysis was last computed |
| `content_hash` | varchar(64) | yes | SHA256 hex of `description + sorted(topics) + readme_excerpt`; drives refresh policy |
| `source` | repo_source enum | no | `manual` (user pasted URL) or `starred` (auto-synced from stars) |
| `is_starred` | boolean | no | True when this repo is currently in the user's GitHub stars |
| `user_id` | bigint FK | no | References `users.telegram_user_id` |
| `last_synced_at` | timestamptz | no | Last time metadata was pulled from GitHub |
| `pending_analysis` | boolean | no | True when LLM analysis was deferred by budget cap; re-queued next day |
| `created_at` | timestamptz | no | Row insertion time |
| `updated_at` | timestamptz | no | Last row modification time |

**Unique constraint:** `(user_id, github_id)` — prevents duplicate rows when a user both stars a repo and manually ingests it; the upsert path converges.

**Indexes:**

- `ix_repositories_user_starred` on `(user_id, is_starred)` — list endpoint filter
- `ix_repositories_user_language` on `(user_id, primary_language)` — language filter
- `ix_repositories_user_pushed_desc` on `(user_id, pushed_at DESC)` — sort by activity
- `ix_repositories_github_id` on `(github_id)` — lookup by GitHub ID during sync upsert

### `repository_embeddings`

Mirrors `SummaryEmbedding` (see `app/db/models/core.py`). One row per repository, enforced by the unique constraint on `repository_id`.

| Column | Type | Nullable | Purpose |
|--------|------|----------|---------|
| `id` | integer PK | no | Surrogate key |
| `repository_id` | integer FK unique | no | References `repositories.id` ON DELETE CASCADE |
| `model_name` | varchar(200) | no | Embedding model identifier |
| `model_version` | varchar(50) | no | Model version; backfill CLI uses this to detect stale vectors |
| `embedding_blob` | bytea | no | Raw float32 embedding serialized to bytes |
| `dimensions` | integer | no | Vector dimensionality |
| `language` | varchar(10) | yes | Language of the embedded text |
| `created_at` | timestamptz | no | Row insertion time |

### `user_github_integrations`

One row per user; enforced by the unique constraint on `user_id`.

| Column | Type | Nullable | Purpose |
|--------|------|----------|---------|
| `id` | integer PK | no | Surrogate key |
| `user_id` | bigint FK unique | no | References `users.telegram_user_id` ON DELETE CASCADE |
| `auth_method` | github_auth_method enum | no | `pat` or `oauth_device` |
| `encrypted_token` | bytea | no | Fernet-encrypted access token (PAT or OAuth); see Encryption section |
| `token_scopes` | varchar(500) | yes | Scopes returned by GitHub when validating the token (e.g., `read:user,public_repo`) |
| `github_login` | varchar(100) | yes | Cached GitHub username from `GET /user`; used in status display |
| `github_user_id` | bigint | yes | GitHub's numeric user ID; stable identifier |
| `status` | github_integration_status enum | no | `active`, `needs_reauth`, or `revoked` |
| `last_synced_at` | timestamptz | yes | Completion time of the most recent sync run |
| `last_sync_cursor` | varchar(500) | yes | Reserved for pagination cursor; currently unused (incremental sync uses `starred_at` comparison) |
| `last_full_sync_at` | timestamptz | yes | Completion time of the most recent full-pagination sync |
| `notified_needs_reauth_at` | timestamptz | yes | When the one-shot `needs_reauth` DM was last sent; prevents repeat spam |
| `created_at` | timestamptz | no | Row insertion time |
| `updated_at` | timestamptz | no | Last row modification time |

**Standalone design choice:** no foreign key to `summaries`. The two content types have different LLM output shapes, different search intents ("find me async web frameworks" vs "summarize this article"), and different list operations ("show all my Python repos" requires querying structured columns, not scanning JSONB summary payloads). Keeping them in separate tables avoids conflating `is_starred` with `is_favorited` and keeps schema evolution independent.

---

## Sync Algorithm

Pseudocode for `_sync_body` in `app/tasks/github_sync.py`:

```
async def sync_body(cfg, db, bot):
    integrations = query(UserGitHubIntegration, status=ACTIVE)
    summary = SyncSummary(...)

    for integration in integrations:
        try:
            token = decrypt_token(integration.encrypted_token)
            client = GitHubAPIClient(token=token, timeout=cfg.github.request_timeout_sec)

            llm_calls_this_run = 0
            seen_github_ids = set()

            async for starred_item in client.list_starred(since=integration.last_full_sync_at):
                # Early-exit on incremental runs
                if integration.last_synced_at and starred_item.starred_at < integration.last_synced_at:
                    break

                repo_dto = starred_item.repo
                seen_github_ids.add(repo_dto.id)
                new_hash = compute_content_hash(repo_dto.description, repo_dto.topics, repo_dto.readme_excerpt)

                existing = query(Repository, user_id=integration.user_id, github_id=repo_dto.id)
                if existing is None or existing.content_hash != new_hash:
                    upsert(Repository, source=STARRED, is_starred=True, content_hash=new_hash, ...)

                    if llm_calls_this_run >= cfg.github.llm_daily_budget:
                        set pending_analysis=True   # deferred; picked up next day
                    else:
                        async with semaphore(cfg.github.llm_concurrency):
                            await analyze_repository(repo_id, force=True)
                            llm_calls_this_run += 1
                else:
                    # metadata refresh only (stars, forks, last_pushed_at); no LLM
                    update_metadata_fields(existing, repo_dto)

            # Soft-unstar: rows in DB but not in this sync's listing
            stale = query(Repository, user_id=integration.user_id, is_starred=True,
                          github_id NOT IN seen_github_ids)
            for repo in stale:
                repo.is_starred = False

            integration.last_synced_at = utcnow()
            if was_full_pagination:
                integration.last_full_sync_at = utcnow()

        except GitHubAuthError:
            integration.status = NEEDS_REAUTH
            if should_notify(integration):   # notified_needs_reauth_at throttle
                await bot.send_message(integration.user_id, "GitHub token expired...")
                integration.notified_needs_reauth_at = utcnow()

        except GitHubRateLimitError as e:
            log.warning("rate_limit", reset_at=e.reset_at, user_id=integration.user_id)
            continue   # skip this user, proceed with next

    return summary
```

The task is registered in `app/tasks/scheduler.py` at `_AppConfigScheduleSource._build_tasks()` using `cfg.github.sync_enabled` and `cfg.github.sync_cron`. The task name is `ratatoskr.github.sync_stars`.

---

## Refresh Logic

Each `Repository` row carries a `content_hash`: a SHA256 hex digest computed from

```
sha256(
    (description or "")
    + "|"
    + "|".join(sorted(topics or []))
    + "|"
    + (readme_excerpt or "")
)
```

Only when this hash changes does the sync re-run LLM analysis and re-embed. Metadata fields (`stars`, `forks`, `watchers`, `pushed_at`) are refreshed unconditionally on every sync without triggering an LLM call. This policy was chosen over time-based or unconditional refresh for two reasons: (1) LLM calls are the dominant marginal cost, so re-running them on unchanged content is wasteful; (2) pure time-based schedules (e.g., "re-analyze every 30 days") produce unnecessary churn on stable repos while missing genuine content changes on active ones.

The topics list is sorted before hashing so topic-set permutations from the GitHub API do not cause spurious re-analysis.

---

## OAuth Device Flow

Standard OAuth Web Flow requires a browser callback URL. Ratatoskr's target deployment (a single-container Raspberry Pi or VPS behind a NAT, accessed via Telegram or a private web UI) typically has no stable public callback URL. OAuth Device Flow solves this: the user visits a short URL on github.com and enters a code; no inbound HTTP connection to the server is required.

**Endpoints** in `app/api/routers/auth/github.py`:

- `POST /v1/auth/github/device/start` — calls `POST https://github.com/login/device/code` with the configured `GITHUB_OAUTH_APP_CLIENT_ID`. Returns `{user_code, verification_uri, device_code, interval, expires_in}` to the client. The server stores `device_code -> user_id` in Redis with a 15-minute TTL. Redis is a hard dependency when Device Flow is enabled; if `REDIS_URL` is unset the endpoint returns 503 with a config-hint body. The PAT path works without Redis.

- `POST /v1/auth/github/device/poll` — accepts `{"device_code": "..."}`. Calls `POST https://github.com/login/oauth/access_token` with `grant_type=urn:ietf:params:oauth:grant-type:device_code`. Maps GitHub's `authorization_pending`, `slow_down`, and `expired_token` error codes to matching response statuses. On success: encrypts the token, upserts `UserGitHubIntegration`, returns `{status: "ok", login: "..."}`. A server-side rate limit prevents polling faster than `interval - 1` seconds per device_code.

The CSRF binding is provided by the Redis mapping: `device_code` is issued by GitHub, stored server-side keyed to the authenticated `user_id`, and validated on poll. A device_code from a different session cannot be used to steal another user's token.

The PAT path (`POST /v1/auth/github/pat`) remains the always-on alternative and requires no Redis or OAuth App registration.

---

## Encryption at Rest

Credentials are encrypted with Fernet (`app/security/token_crypto.py`), a symmetric AEAD construction using AES-128-CBC + HMAC-SHA256.

- The key is a 32-byte URL-safe base64 string stored in `GITHUB_TOKEN_ENCRYPTION_KEY`.
- Production/public startup requires `GITHUB_TOKEN_ENCRYPTION_KEY`, so a self-hosted deployment cannot accept or sync GitHub credentials without a deployment-owned encryption key.
- The Fernet instance is lazy-loaded and cached via `@lru_cache(maxsize=1)`. The first call to `encrypt_token` or `decrypt_token` validates the key; a missing or malformed key raises `MissingEncryptionKeyError` immediately with a hint to run `python tools/scripts/generate_github_encryption_key.py`.
- Ciphertext is stored in the `encrypted_token` column as `bytea` — a raw byte sequence, not a base64 string. The Postgres column type is `LargeBinary` in SQLAlchemy.
- Token values never appear in API responses, log output, validation error details, or trace attributes generated by this subsystem. The GitHub auth responses return only login/status/scope warnings, validation errors omit rejected request input, `logging_utils.py` redacts token-like field names and GitHub token string patterns, and OpenTelemetry startup forces HTTP header sanitizers for authorization, token, and secret-like fields before instrumentation starts.

**Key generation:**

```bash
python tools/scripts/generate_github_encryption_key.py
# or equivalently:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Copy the output into your `.env` as `GITHUB_TOKEN_ENCRYPTION_KEY=<key>`.

**Key rotation:** `GITHUB_TOKEN_PREVIOUS_KEYS` is a comma-separated list of older Fernet keys. `MultiFernet` decrypts with the primary key first and then previous keys, while new writes always use the primary key. Run `python -m app.cli.rotate_github_tokens` after deploying a new primary key, then remove retired keys once all rows have been re-encrypted.

---

## Cost Model

Each LLM analysis call consumes roughly 1000-2000 input tokens (system prompt + repo metadata + README excerpt) and 300-800 output tokens. At $0.01-0.03 per 1000 tokens with typical OpenRouter models, a single analysis costs $0.01-0.05.

For a user with 1000 starred repositories on first sync:

- Default `GITHUB_LLM_DAILY_BUDGET=100` means 100 repos analyzed per day.
- Full analysis of 1000 repos takes 10 days.
- Total first-sync cost: approximately $10-50, spread over 10 days.

The `GITHUB_LLM_CONCURRENCY` cap (default 2) limits simultaneous LLM calls within a single sync run, both to respect OpenRouter rate limits and to avoid saturating the event loop.

Repos deferred by the budget cap have `pending_analysis=true`. The next day's cron run picks them up. The web UI surfaces this state as a "still indexing" badge on the repository card so users understand why search results may be incomplete immediately after connecting a large GitHub account.

---

## Search Semantics

Repository embeddings share the Qdrant collection with summary embeddings, using an `entity_type="repository"` payload discriminator. Immediate writes and CocoIndex reconciliation both use `repository_point_id(environment, user_scope, repository_id)` from `app/infrastructure/vector/point_ids.py`, so repeated exports are idempotent. The `RepositorySearchService` (`app/infrastructure/search/repository_search_service.py`) injects this filter on every query automatically so repository searches never return summary results and vice versa.

Every query is also hard-scoped to the authenticated user's `user_id`:

1. **Qdrant filter:** `must: [{key: "user_id", match: {value: <user_id>}}, {key: "entity_type", match: {value: "repository"}}]`
2. **Postgres hydration:** the returned IDs are re-queried with an additional `WHERE user_id = <user_id>` clause as defense-in-depth against Qdrant index inconsistencies.

Results below `min_similarity` (configurable, default 0.5) are dropped before hydration. The search endpoint is `GET /v1/search/repositories?q=<query>`.

---

## Configuration

All variables are read by `app/config/github.py::GitHubConfig`.

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `GITHUB_REQUEST_TIMEOUT_SEC` | `30.0` | No | HTTP timeout in seconds for all GitHub API calls |
| `GITHUB_README_MAX_BYTES` | `51200` | No | Maximum README size (bytes) to fetch and store; content is truncated at character boundary |
| `GITHUB_CONCURRENCY_PER_USER` | `2` | No | Maximum concurrent GitHub API requests per user during sync |
| `GITHUB_OAUTH_APP_CLIENT_ID` | _(none)_ | No — OAuth Device Flow only | GitHub OAuth App client ID; PAT path works without this |
| `GITHUB_OAUTH_APP_CLIENT_SECRET` | _(none)_ | No — OAuth Device Flow only | GitHub OAuth App client secret; stored as `SecretStr`, never logged |
| `GITHUB_TOKEN_ENCRYPTION_KEY` | _(none)_ | Yes — in production/public mode; also required before any token can be stored elsewhere | 32-byte URL-safe base64 Fernet key; generate with `tools/scripts/generate_github_encryption_key.py` |
| `GITHUB_TOKEN_PREVIOUS_KEYS` | _(none)_ | No — key rotation only | Comma-separated previous Fernet keys accepted for decrypting existing rows during rotation |
| `GITHUB_SYNC_ENABLED` | `true` | No | Master switch for the daily Taskiq sync job |
| `GITHUB_SYNC_CRON` | `0 2 * * *` | No | UTC cron expression for the sync job; default is 02:00 UTC |
| `GITHUB_LLM_CONCURRENCY` | `2` | No | Maximum concurrent LLM analysis calls within a single sync run |
| `GITHUB_LLM_DAILY_BUDGET` | `100` | No | Maximum LLM calls per day across all sync runs; excess repos get `pending_analysis=true` |

---

## Future Work

The following are explicitly out of scope for the current implementation and tracked as follow-up items:

- **Repository recommendations** — use embedding-neighbor queries to surface repos similar to ones the user has starred or manually ingested.
- **Topic clustering and trend dashboards** — group repositories by inferred topic cluster; surface emerging technology signals over time.
- **Dependency-graph extraction** — parse `package.json`, `Cargo.toml`, `pyproject.toml`, and `go.mod` to build a language-aware dependency graph.
- **Code search** — index file-level content beyond README for fine-grained search.
- **Multi-account GitHub support** — currently one `UserGitHubIntegration` row per user enforced by the unique constraint; multi-account would require lifting that.
- **OAuth Web Flow** — explicitly rejected for the self-hosted deployment shape; reconsider if a hosted-cloud variant is ever built.
- **Re-analysis prompt versioning UI** — surface `analysis_model` to allow bulk re-analyze on prompt iterations.
