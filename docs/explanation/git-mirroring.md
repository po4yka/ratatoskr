# On-Disk Git Mirroring

How Ratatoskr produces and maintains local bare-clone backups of git repositories: architecture, data model, credential handling, failure tracking, and operational surfaces.

**Audience:** Contributors extending or debugging the subsystem, operators configuring storage or scheduling. **Type:** Explanation. **Related:** [`docs/SPEC.md`](../SPEC.md), [`docs/explanation/github-repository-ingestion.md`](github-repository-ingestion.md), [`docs/reference/environment-variables.md`](../reference/environment-variables.md).

---

## Overview

Ratatoskr's GitHub Repository Ingestion subsystem (see [`github-repository-ingestion.md`](github-repository-ingestion.md)) fetches GitHub metadata — description, topics, README excerpt — via the REST API and stores the result in PostgreSQL for LLM analysis and semantic search. It does not preserve repository history or file content. The git-mirroring subsystem complements this by running `git clone --mirror`, which captures the complete on-disk state of a repository: all refs (`heads/`, `tags/`, `remotes/`), loose objects, packfiles, and the full commit graph. This is a true backup, not a metadata snapshot.

The subsystem is a port of [gitout](https://github.com/nicholasgasior/gitout), a Kotlin CLI tool that provides the core engine modules: error categorization, adaptive retry with backoff, storage circuit breaking, post-sync maintenance, LFS handling, and README extraction from bare clones. These modules have been translated to Python and wired into Ratatoskr's async infrastructure — Taskiq scheduler, SQLAlchemy persistence, Fernet-encrypted credential store — while preserving the original engine contracts.

---

## What Gets Mirrored

Three source types feed the sync job.

**GitHub-linked repositories** (`source=github`) are repos that already have a row in the `repositories` table from the GitHub ingestion subsystem. When the Telegram `/mirror` command or the REST `POST /v1/git-mirrors` endpoint registers a mirror, `GitMirrorRepository.upsert_target` creates a `git_mirrors` row with `source=github` and a foreign key to the matching `repositories.id`. During sync, `GitMirrorService._resolve_url` looks up the user's `UserGitHubIntegration`, decrypts the stored token, and injects it into the clone URL (see Credentials below).

**GitHub gists** (`source=github`, clone URL `https://gist.github.com/<id>.git`) are enumerated automatically when `GIT_BACKUP_MIRROR_GISTS=true`. At the start of each sync run, `_enumerate_and_upsert_gists` queries all active `UserGitHubIntegration` rows, calls `GET /gists` for each user (with Link-header pagination), and upserts a `git_mirrors` row per gist via `GitMirrorRepository.upsert_target`. Freshly upserted rows receive `status=pending` and are picked up by `perform_sync` in the same run. Errors for one user (API failure, decryption failure) are logged and skipped; they do not abort the run for other users. Gist credentials are injected the same way as repository credentials — the `gist.github.com` host is in the `_GITHUB_HOSTS` allowlist in `app/core/git_url_safety.py`. On disk, gists land under `<data_path>/github/gist.github.com/<name>.git`, separate from regular repos under `<data_path>/github/github.com/<name>.git`, so the two namespaces never collide.

**Manual/arbitrary repositories** (`source=manual`) are any git-accessible URL that the user registers directly — public GitHub repos without a GitHub integration, self-hosted Gitea/Forgejo instances, or any other `https://` or `git://` URL. These receive a `git_mirrors` row with no `repository_id` FK and are cloned without credentials. The static `GIT_BACKUP_EXTRA_REPOS` config key (a name-to-URL dict in `ratatoskr.yaml`) also produces manual mirrors, upserting rows at job startup so outcomes are persisted identically to user-registered mirrors.

---

## Architecture

### Sync entry point

The Taskiq job `ratatoskr.git_backup.sync` (`app/tasks/git_backup_sync.py`) is the sole scheduled entry point. It acquires a Redis distributed lock keyed `task_lock:git_backup_sync` (TTL 3600 s) before doing any work, so concurrent scheduler firings do not double-clone. The job is gated by `GIT_BACKUP_ENABLED`; when false it returns immediately without acquiring the lock. The cron expression defaults to `0 4 * * *` (04:00 UTC) and is overridden with `GIT_BACKUP_SYNC_CRON`.

### GitMirrorService orchestration

`GitMirrorService` (`app/adapters/git_backup/mirror_service.py`) is the central orchestrator. It is fully injectable: every collaborator — retry policy, circuit breaker, maintenance, LFS support, git runner — has a default that is constructed from `GitBackupConfig` when not supplied, making the class straightforward to unit-test with fakes.

`perform_sync` runs four sequential phases:

1. **Preflight storage check.** Before any git operation, `_preflight_storage_check` writes a sentinel file to `GIT_BACKUP_DATA_PATH`, reads it back, and deletes it. A failure (missing directory, permission error, I/O error, or timeout) aborts the entire run with a `RuntimeError` rather than discovering mid-run that the volume is absent or read-only.

2. **Task collection.** `_collect_tasks` queries `GitMirrorRepository.list_due` for all eligible `git_mirrors` rows, resolves credentials for each, determines the on-disk destination path, and classifies repos above `GIT_BACKUP_LARGE_REPO_THRESHOLD_KB` as large. Static `extra_repos` entries are upserted and appended.

3. **Parallel execution.** All tasks are submitted to `asyncio.gather`. Two semaphores bound concurrency: `asyncio.Semaphore(GIT_BACKUP_WORKERS)` wraps every task; `asyncio.Semaphore(GIT_BACKUP_LARGE_REPO_MAX_PARALLEL)` additionally gates large-repo initial clones so they do not saturate bandwidth simultaneously. Large repos also receive a timeout multiplied by `GIT_BACKUP_LARGE_REPO_TIMEOUT_MULTIPLIER`.

4. **Outcome persistence.** Each `MirrorOutcome` is written back to the DB via `GitMirrorRepository.record_success`, `record_failure`, or `record_skip`.

### Engine modules (ported from gitout)

| Module | File | Role |
|--------|------|------|
| `errors` | `app/adapters/git_backup/errors.py` | `ErrorCategory` enum + `classify(message)` function. Maps git stderr output to nine categories: `HTTP2_ERROR`, `NETWORK_ERROR`, `TIMEOUT`, `AUTH_ERROR`, `REPOSITORY_ERROR`, `STORAGE_ERROR`, `SSL_ERROR`, `RATE_LIMIT`, `UNKNOWN`. Classification order is significant — HTTP/2 patterns take priority over generic network patterns; connection-timed-out is classified as `NETWORK_ERROR` before the generic timeout check. |
| `retry` | `app/adapters/git_backup/retry.py` | `RetryPolicy` with linear, exponential, or constant backoff. Default: 6 attempts, 5 s base delay, linear. The `adaptive_retry` flag applies category-specific delay multipliers (`RATE_LIMIT` → ×3.0, `NETWORK_ERROR` → ×2.0, `TIMEOUT` → ×1.5) and triggers an HTTP/1.1 fallback on the next attempt when the category is `HTTP2_ERROR` or `NETWORK_ERROR`. Non-retryable categories (`AUTH_ERROR`, `STORAGE_ERROR`, `SSL_ERROR`, `REPOSITORY_ERROR`) abort immediately. |
| `circuit_breaker` | `app/adapters/git_backup/circuit_breaker.py` | `StorageCircuitBreaker` trips after `threshold` (default 3) consecutive `STORAGE_ERROR` failures. Once open it causes all remaining tasks in the run to be skipped rather than attempting git operations against a volume that is likely full or unmounted. Non-storage failures reset the consecutive streak; once open the breaker stays open for the duration of the run. |
| `maintenance` | `app/adapters/git_backup/maintenance.py` | `RepositoryMaintenance` runs post-sync maintenance on each bare clone. Three strategies: `gc-auto` (`git gc --auto`), `geometric` (`git repack --geometric=2 -d`), and `none`. Optionally writes a commit-graph (`git commit-graph write --reachable`) after every sync. A periodic full repack (`git repack -a -d`) runs every 7 or 30 syncs when `GIT_BACKUP_FULL_REPACK_INTERVAL` is `weekly` or `monthly`. Maintenance runs in `asyncio.to_thread` so it does not block the event loop. |
| `lfs` | `app/adapters/git_backup/lfs.py` | `LfsSupport` detects LFS-enabled bare repos (presence of `lfs/` directory or `filter=lfs` in `HEAD:.gitattributes`) and runs `git lfs fetch --all` when `GIT_BACKUP_FETCH_LFS=true`. `git clone --mirror` stores only LFS pointer files, so this step is required to back up the actual binary content. LFS availability is checked at service construction time; if `git lfs version` fails the module is disabled silently. |
| `readme_extractor` | `app/adapters/git_backup/readme_extractor.py` | `ReadmeExtractor` extracts the first README found in a bare clone (`README.md`, `readme.md`, `README.rst`, `README.txt`, `README`) via `git --git-dir=<path> show HEAD:<name>`, truncated to 8000 characters. Used by higher-level flows that want to surface README content without a full checkout; the core sync job does not call this directly. |

---

## Data Model

The `git_mirrors` table (`app/db/models/git_backup.py`) holds one row per (user, clone URL) pair. The unique constraint `uq_git_mirrors_user_clone_url` on `(user_id, clone_url)` prevents duplicate registrations for the same URL.

| Column | Type | Nullable | Purpose |
|--------|------|----------|---------|
| `id` | integer PK | no | Auto-increment surrogate key |
| `user_id` | bigint FK | no | References `users.telegram_user_id` ON DELETE CASCADE |
| `repository_id` | integer FK | yes | References `repositories.id` ON DELETE SET NULL; populated for GitHub-linked mirrors, null for manual |
| `source` | `git_mirror_source` enum | no | `github` or `manual` |
| `clone_url` | varchar(1000) | no | Canonical https:// clone URL stored without credentials |
| `name` | varchar(320) | yes | Human-readable label (owner/repo for GitHub mirrors, user-supplied for manual) |
| `mirror_path` | varchar(1000) | yes | Absolute path to the bare clone on disk; null until first successful sync |
| `status` | `git_mirror_status` enum | no | `pending`, `ok`, `failed`, `skipped`, or `excluded` |
| `default_branch` | varchar(200) | yes | Default branch, populated after first sync |
| `size_kb` | bigint | yes | On-disk size of the bare clone in KB; updated on each successful sync |
| `last_mirrored_at` | timestamptz | yes | Timestamp of most recent successful mirror operation |
| `last_attempt_at` | timestamptz | yes | Timestamp of most recent attempt (success or failure) |
| `consecutive_failures` | integer | no | Counter reset to 0 on success; drives cooldown logic |
| `last_error` | text | yes | Truncated stderr output from the last failed attempt (max 4000 chars) |
| `last_error_category` | varchar(50) | yes | `ErrorCategory.value` string from the last failure |
| `backoff_until` | timestamptz | yes | When set, the mirror is skipped by `list_due` until this time passes |
| `excluded_at` | timestamptz | yes | Set when the mirror is tombstoned (`status=excluded`). Null for all other statuses. |
| `clone_strategy` | varchar(50) | yes | Clone strategy used for the most recent initial clone: `"full"` (mirror) or `"shallow"` (`--depth=1`). Written by `record_success` / `record_failure`. Null for rows that pre-date the shallow-clone feature or for update (non-clone) operations. |
| `created_at` | timestamptz | no | Row insertion time |
| `updated_at` | timestamptz | no | Last modification time |

**Indexes:**

- `ix_git_mirrors_user_status` on `(user_id, status)` — list endpoint and eligibility filter
- `ix_git_mirrors_repository_id` on `(repository_id)` — join to `repositories` table

### Eligibility and cooldown

`GitMirrorRepository.list_due` returns mirrors whose status is `pending`, `ok`, or `failed`, minus those in active cooldown. A mirror enters cooldown when `GIT_BACKUP_AUTO_SKIP_FAILING=true` and `consecutive_failures >= GIT_BACKUP_MAX_CONSECUTIVE_FAILURES` (default 5): `backoff_until` is set to `now + GIT_BACKUP_FAILURE_COOLDOWN_HOURS` (default 24 h). The `skipped` status is written per-run without resetting failure counters; `ok` resets `consecutive_failures` to 0 and clears `backoff_until`.

### Tombstoning (excluded status)

When a git clone attempt fails with a signal that unambiguously means the remote repository has been permanently deleted or renamed — `"repository not found"`, `"does not exist"`, `"could not find repository"`, HTTP 404, or HTTP 410 — the mirror is tombstoned: `status` is set to `excluded` and `excluded_at` is set to the current timestamp. Tombstoned mirrors are never returned by `list_due`, so they do not cycle through the FAILED cooldown loop.

The conservative detection function `is_permanently_gone` (`app/adapters/git_backup/errors.py`) rejects any message that also contains an auth signal (`authentication failed`, `permission denied`, `403`, etc.), because a private repository returning 404 to an unauthenticated clone is an auth problem, not a permanent deletion.

A tombstoned mirror can be revived by the user re-adding the same URL via `/mirror` or the `POST /v1/git-mirrors` API endpoint. `GitMirrorRepository.upsert_target` detects the `excluded` status on the existing row and resets it to `pending`, clearing `excluded_at`, `consecutive_failures`, `backoff_until`, and `last_error` so the next sync cycle retries from a clean state.

---

## Credentials

Credentials for GitHub-linked mirrors are sourced from `UserGitHubIntegration.encrypted_token`, which is encrypted at rest with Fernet using `GITHUB_TOKEN_ENCRYPTION_KEY` — the same key used by the GitHub repository ingestion subsystem. `decrypt_secret` (`app/security/secret_crypto.py`) decrypts the token at sync time. The plaintext token is then embedded in the clone URL as `https://x-access-token:<percent-encoded-token>@github.com/...` via `_inject_token_into_url`, which is the only form git accepts without interactive prompting. The raw token is never logged; `_redact_url` replaces the credential segment with `***@` before any log output.

If the `UserGitHubIntegration` row is missing or decryption fails (e.g. key rotation not yet completed), the mirror falls back to the unauthenticated clone URL and continues rather than aborting the run. This allows public repos to succeed while private repos will produce an `AUTH_ERROR` that is categorized, logged, and recorded in `last_error_category`.

Manual and `extra_repos` mirrors are cloned unauthenticated. The clone URL is used exactly as registered.

---

## Surfaces

### Taskiq job

The job `ratatoskr.git_backup.sync` runs on the cron from `GIT_BACKUP_SYNC_CRON` when `GIT_BACKUP_ENABLED=true`. It logs `git_backup_sync_disabled` (and returns early) when the flag is off, `git_backup_sync_skipped_lock_held` when another instance holds the lock, and `git_backup_sync_complete` with `ok`, `failed`, `skipped`, and `total` counts on normal completion.

### Telegram commands

`/mirror <url>` — register a git URL for mirroring and defer the first sync to the next scheduled run. The bot creates a `git_mirrors` row with `status=pending` and responds with a confirmation. `/mirrors` — list all registered mirrors for the user with their current status and size.

### REST API (`/v1/git-mirrors`)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/v1/git-mirrors` | List all mirrors for the authenticated user |
| `POST` | `/v1/git-mirrors` | Register a new mirror; returns 202 and triggers a deferred sync |
| `GET` | `/v1/git-mirrors/{id}` | Retrieve a single mirror by ID |
| `DELETE` | `/v1/git-mirrors/{id}` | Delete the `git_mirrors` row; returns 204. Does **not** remove the on-disk bare clone (see Known Deferrals). |

---

## Operations

### Storage

Bare clones land under `GIT_BACKUP_DATA_PATH` (default `/data/git-mirrors`). The path is derived by `_mirror_destination` and stored in `mirror_path` after first sync, so the path is stable across service restarts.

- **GitHub repository mirrors** (`source=github`, clone URL on `github.com`): `<data_path>/github/github.com/<name>.git`
- **GitHub gist mirrors** (`source=github`, clone URL on `gist.github.com`): `<data_path>/github/gist.github.com/<name>.git`
- **Manual mirrors** (`source=manual`): `<data_path>/manual/<name>.git`

The host-based subdirectory (`github.com` vs `gist.github.com`) ensures that a gist and a regular repo can share the same human-readable name without colliding on disk.

In Docker Compose the worker container mounts a named volume `git_mirrors_data` at the configured path. The volume must be declared in `ops/docker/docker-compose.yml` and should be bind-mounted to a host directory for durability across container replacements.

### Runtime dependencies

The container image must have `git` available on `PATH`. `resolve_git_executable` (`app/adapters/git_backup/git_exec.py`) probes for `git` and raises early if absent. `git-lfs` is additionally required when `GIT_BACKUP_FETCH_LFS=true`; absence causes `LfsSupport.is_lfs_available()` to return false and LFS fetching to be disabled silently.

### Configuration

All variables are read by `app/config/git_backup.py::GitBackupConfig`. Full reference: [`docs/reference/environment-variables.md`](../reference/environment-variables.md).

| Variable | Default | Description |
|----------|---------|-------------|
| `GIT_BACKUP_ENABLED` | `false` | Master switch; job is not registered with the scheduler when false |
| `GIT_BACKUP_SYNC_CRON` | `0 4 * * *` | UTC cron expression for the sync job |
| `GIT_BACKUP_DATA_PATH` | `/data/git-mirrors` | Root directory for bare clones |
| `GIT_BACKUP_WORKERS` | `4` | Parallel clone/fetch worker count (1–32) |
| `GIT_BACKUP_REPO_TIMEOUT_SECONDS` | `3600` | Per-repository operation timeout in seconds |
| `GIT_BACKUP_FETCH_LFS` | `false` | Fetch LFS objects after mirroring |
| `GIT_BACKUP_MAINTENANCE_STRATEGY` | `gc-auto` | Post-sync maintenance: `gc-auto`, `geometric`, or `none` |
| `GIT_BACKUP_FULL_REPACK_INTERVAL` | `never` | Periodic full repack: `never`, `weekly`, or `monthly` |
| `GIT_BACKUP_WRITE_COMMIT_GRAPH` | `true` | Write commit-graph after each sync |
| `GIT_BACKUP_LARGE_REPO_THRESHOLD_KB` | `512000` | Size in KB above which large-repo handling applies |
| `GIT_BACKUP_LARGE_REPO_TIMEOUT_MULTIPLIER` | `3` | Timeout multiplier for large repos |
| `GIT_BACKUP_LARGE_REPO_MAX_PARALLEL` | `2` | Maximum concurrent large-repo initial clones |
| `GIT_BACKUP_MAX_CONSECUTIVE_FAILURES` | `5` | Failures before cooldown activates |
| `GIT_BACKUP_FAILURE_COOLDOWN_HOURS` | `24` | Cooldown window after max failures |
| `GIT_BACKUP_VERIFY_CERTIFICATES` | `true` | When `false`, passes `http.sslVerify=false` to git; disables TLS certificate verification |
| `GIT_BACKUP_POST_BUFFER_SIZE` | `524288000` | git `http.postBuffer` in bytes (500 MB) |
| `GIT_BACKUP_LOW_SPEED_LIMIT` | `1000` | git `http.lowSpeedLimit` in bytes/second; `0` disables |
| `GIT_BACKUP_LOW_SPEED_TIME` | `60` | git `http.lowSpeedTime` in seconds (used when low_speed_limit > 0) |
| `GIT_BACKUP_SINGLE_BRANCH_ONLY` | `false` | Use `git clone --bare --single-branch` instead of `--mirror` |
| `GIT_BACKUP_SHALLOW_CLONE_THRESHOLD_KB` | `0` | Size threshold in KB for automatic shallow clone; `0` = disabled |
| `GIT_BACKUP_SHALLOW_CLONE_AFTER_FAILURES` | `0` | Consecutive failure count that triggers shallow clone; `0` = disabled |
| `GIT_BACKUP_AUTO_SKIP_FAILING` | `true` | Skip mirrors in cooldown window instead of retrying |
| `GIT_BACKUP_MIRROR_GISTS` | `false` | When `true`, enumerate all gists per active GitHub integration and upsert `git_mirrors` rows for them |
| `GIT_BACKUP_EXTRA_REPOS` | `{}` | Static name→URL map for repos without a DB row; set via `ratatoskr.yaml` |

---

## SSL and HTTP Tuning

All git operations are wrapped with a fixed set of `-c` flags built by `build_git_command` (`app/adapters/git_backup/git_commands.py`). The flags correspond to gitout's `ssl.*` and `http.*` config sections:

- **`http.sslVerify`** — emitted as `http.sslVerify=false` only when `GIT_BACKUP_VERIFY_CERTIFICATES=false`. Default (`true`) omits the flag, so git uses its compiled-in CA bundle. Only disable on fully private deployments where the server presents a self-signed certificate; disabling TLS verification exposes clones to MITM attacks.
- **`http.postBuffer`** — always emitted (default 524 288 000 bytes = 500 MB). Controls the in-memory send buffer for HTTP POST operations. Increase when seeing `RPC failed; HTTP 411 Caused by: send-pack: unexpected disconnect` errors on large repos.
- **`http.lowSpeedLimit` / `http.lowSpeedTime`** — emitted when `GIT_BACKUP_LOW_SPEED_LIMIT > 0` (default 1000 bytes/s, 60 s). Causes git to abort a transfer that stays below the limit for the configured number of seconds. Set `GIT_BACKUP_LOW_SPEED_LIMIT=0` to disable.
- **`http.version`** — forced to `HTTP/1.1` by default (matching gitout's default). The retry policy promotes an HTTP/1.1 fallback on `HTTP2_ERROR` categories; `force_http1` on the retry context overrides any per-run HTTP/2 setting.
- **`http.followRedirects=false`** — always set by the SSRF hardening layer (`disable_redirects=True`) to prevent a trusted host from 30x-redirecting git to an internal endpoint.

### Shallow-clone strategy

By default, all clones use `git clone --mirror`, which preserves the complete ref history. Two opt-in mechanisms (both disabled by default with `0`) switch initial clones to `git clone --depth=1 --single-branch`:

1. **Size-based** (`GIT_BACKUP_SHALLOW_CLONE_THRESHOLD_KB`): when the stored `size_kb` for the mirror meets or exceeds the threshold.
2. **Failure-based** (`GIT_BACKUP_SHALLOW_CLONE_AFTER_FAILURES`): when `consecutive_failures` meets or exceeds the threshold.

When both thresholds are configured (non-zero), **both conditions must be met** (gitout AND semantics). When only one is configured, that condition alone governs. The chosen strategy — `"shallow"` or `"full"` — is persisted to the `clone_strategy` column of `git_mirrors` after each initial clone so it can be queried. Shallow clones are never applied to `git remote update` (repo already exists on disk).

Setting `GIT_BACKUP_SINGLE_BRANCH_ONLY=true` uses `git clone --bare --single-branch` regardless of the shallow-clone logic; the two options are mutually exclusive (shallow takes priority if `use_shallow_clone` is true).

---

## Health monitoring

The sync job supports a Healthchecks.io-compatible dead-man-switch via `GIT_BACKUP_HC_PING_URL`. When set, the Taskiq task (`app/tasks/git_backup_sync.py`) performs three best-effort HTTP pings around the sync:

| Event | Endpoint | Meaning |
|-------|----------|---------|
| Before `perform_sync` begins | `POST {url}/start` | Job is running; reset the "grace" timer |
| After `perform_sync` returns | `POST {url}` | Job completed successfully |
| If `perform_sync` raises | `POST {url}/fail` | Job failed; trigger alert |

Ping semantics mirror [gitout's `health_check.py`](https://github.com/nicholasgasior/gitout): a ping is sent on completion regardless of per-repository failures (`summary.failed > 0`) because the job itself ran to completion. Only an unhandled exception (e.g. storage preflight failure, Redis lock error) routes to the `/fail` endpoint.

All pings are best-effort: network errors and timeouts are logged at WARNING and swallowed. A failed ping never affects the backup outcome. The ping timeout is configured with `GIT_BACKUP_HC_PING_TIMEOUT_SECONDS` (default `10.0` s).

Implementation: `app/adapters/git_backup/health_ping.py` — three module-level async functions (`ping_start`, `ping_success`, `ping_failure`) each backed by a short-lived `httpx.AsyncClient`.

---

## Known Deferrals

**On-disk cleanup on DELETE.** `DELETE /v1/git-mirrors/{id}` removes the `git_mirrors` row but leaves the bare clone directory in place. Automatic on-disk cleanup on deletion is tracked as a follow-up item; operators should remove stale directories manually.

**README semantic search for manual mirrors.** GitHub-linked mirrors whose `repositories` row already has `analysis_json` are searchable via the existing repository embedding index. Manual mirrors (`source=manual`) with no `repository_id` FK have no such entry; `ReadmeExtractor` can surface their README content but wiring that content into the embedding pipeline for arbitrary-URL mirrors is deferred.

---

## Cross-references

- GitHub metadata ingestion (API-only, no history): [`docs/explanation/github-repository-ingestion.md`](github-repository-ingestion.md)
- All `GIT_BACKUP_*` env vars: [`docs/reference/environment-variables.md`](../reference/environment-variables.md)
- Data model overview: [`docs/reference/data-model.md`](../reference/data-model.md)
- Source — service: [`app/adapters/git_backup/mirror_service.py`](../../app/adapters/git_backup/mirror_service.py)
- Source — persistence: [`app/adapters/git_backup/repository.py`](../../app/adapters/git_backup/repository.py)
- Source — config: [`app/config/git_backup.py`](../../app/config/git_backup.py)
- Source — model: [`app/db/models/git_backup.py`](../../app/db/models/git_backup.py)
- Source — task: [`app/tasks/git_backup_sync.py`](../../app/tasks/git_backup_sync.py)
