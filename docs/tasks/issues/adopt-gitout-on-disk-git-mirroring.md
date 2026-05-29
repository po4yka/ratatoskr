---
title: Adopt gitout on-disk git mirroring into ratatoskr
status: active
area: backend
priority: high
owner: unassigned
blocks: []
blocked_by: []
created: 2026-05-29
updated: 2026-05-29
---

- [ ] #task Adopt gitout on-disk git mirroring into ratatoskr #repo/ratatoskr #area/backend #status/active 🔼

## Objective

Fold the [gitout](https://github.com/po4yka/gitout) git-backup tool's functionality into ratatoskr as a first-class subsystem rather than running it as a separate service. ratatoskr already covers gitout's GitHub-API discovery, repo metadata/README/LLM analysis, and semantic search over repos; what it entirely lacks is gitout's core: **on-disk `git clone --mirror` of full git history (refs + objects + packs)** plus the mature resilience/maintenance/LFS engine around it. This task ports that engine and re-expresses gitout's CLI/TOML/cron/notifications using ratatoskr's existing patterns (pydantic settings, Taskiq scheduler, Telegram command system, Qdrant/embedding infra).

Decisions locked (2026-05-29): full engine port; mirror both GitHub-linked repos AND arbitrary git URLs; dedicated `GitMirror` table (not columns on `Repository`); expose via scheduled Taskiq job + Telegram commands + REST endpoints.

## Context

gitout source: `~/GitRep/gitout/` (Python 3.11, Typer CLI, httpx; deps deliberately minimal). ratatoskr is the target backend.

Confirmed boundaries from exploration:
- ratatoskr does **NOT** clone git repos to disk anywhere — no `git clone`, `--mirror`, GitPython/pygit2/dulwich, no `mirror_path` column. The mirror path is purely additive (no duplication risk).
- ratatoskr GitHub ingestion is REST-API-only: `app/adapters/github/github_api_client.py`, daily stars sync `app/tasks/github_sync.py` (`ratatoskr.github.sync_stars`, cron `GITHUB_SYNC_CRON` default `0 2 * * *`), models `app/db/models/repository.py` (`Repository`, `RepositoryEmbedding`, `UserGitHubIntegration`), per-user Fernet-encrypted tokens (`GITHUB_TOKEN_ENCRYPTION_KEY`, `MultiFernet` rotation).
- Vector/embedding infra to REUSE (do not bring gitout's own clients): `QdrantVectorStore` (`app/infrastructure/vector/qdrant_store.py`, built via `app/di/shared.py:build_qdrant_vector_store`), `GeminiEmbeddingService`/`create_embedding_service` (`app/infrastructure/embedding/`), deterministic point IDs `app/infrastructure/vector/point_ids.py` (`str_to_uuid` = uuid5/OID), `RepositoryEmbeddingGenerator`, `RepositorySearchService`, `VectorIndexedEntityAdapter` reconciliation protocol (`app/infrastructure/vector/reconciliation.py`).
- Extension-point patterns confirmed: Telegram command via `TelegramCommandContribution` route table wired in `app/di/telegram_commands.py`; Taskiq task via `@broker.task(task_name=...)` + Redis distributed lock + `ScheduledTask` registration in `app/tasks/scheduler.py`; pydantic `BaseModel` sub-config per file under `app/config/` wired into `AppConfig`/`Settings`; `Database` (`app/db/session.py`) is the sole DB entry point; `ALL_MODELS` registration in `app/db/models/__init__.py` then Alembic autogenerate.
- Runtime Docker image (`ops/docker/Dockerfile` stage 2) lacks `git` and `git-lfs` — must be added; a persistent volume for mirror storage must be mounted.

## gitout module disposition

PORT (new high-value core → `app/adapters/git_backup/`): `git_commands.py`, `git_exec.py`, `retry.py`, `errors.py`, `circuit_breaker.py`, `maintenance.py`, `lfs.py`, `search/readme_extractor.py`; plus the hermetic engine/resilience tests.
ADAPT: `engine.py` → `GitMirrorService` (keep asyncio.Semaphore pool + preflight storage check; swap discovery for ratatoskr repo repository + config URLs; swap credential-store temp file for the existing Fernet-decrypted integration token); `failure_tracker.py` + `state_tracker.py` → state moves from JSON files into Postgres; `search/index_service.py` → call `RepositoryEmbeddingGenerator` + `RepositorySearchService`.
DROP: `github.py`/`github_client.py` (GraphQL — ratatoskr REST wins), `search/gemini.py`/`search/qdrant.py` (reuse ratatoskr infra), `config.py` (TOML → pydantic), `cli.py` (→ commands/REST/Taskiq), `cron.py` (→ Taskiq scheduler), `telegram.py`/`health_check.py` (→ ratatoskr Telegram/observability), `automation/` (unrelated tooling).

## Scope (phased)

### Phase 1 — Foundations
- [ ] `app/config/git_backup.py`: `GitBackupConfig` (frozen BaseModel) — `enabled` (`GIT_BACKUP_ENABLED`), `sync_cron` (`GIT_BACKUP_SYNC_CRON`, default `0 4 * * *`, 5-field validator), `data_path` (`GIT_BACKUP_DATA_PATH`, default `/data/git-mirrors`), `workers` (1–32, default 4), `repository_timeout_seconds`, `fetch_lfs`, maintenance (`strategy` gc-auto/geometric/none, `full_repack_interval`), large-repo thresholds, failure-tracking knobs. Wire into `AppConfig`/`Settings`/`as_app_config()`.
- [ ] `app/db/models/git_backup.py`: `GitMirror` (id, optional `repository_id` FK→repositories.id nullable, `source` enum github/manual, `clone_url`, `mirror_path`, `status` enum, `last_mirrored_at`, `size_kb`, `default_branch`, `consecutive_failures`, `last_error`, `last_error_category`, `backoff_until`, `clone_strategy`, `user_id` FK, timestamps; unique on (user_id, clone_url)). Optional `GitMirrorRun` (per-attempt audit row). Register in `ALL_MODELS`.
- [ ] Alembic migration for the new table(s) + indexes.
- [ ] Dockerfile: add `git git-lfs` to runtime stage; declare/mount a `/data/git-mirrors` volume; document in compose.

### Phase 2 — Engine port (`app/adapters/git_backup/`)
- [ ] Port `git_commands.py` (build `git clone --mirror` / `git remote update` argv), `git_exec.py` (resolve git executable), `errors.py` (ErrorCategory + classify), `retry.py` (RetryPolicy adaptive), `circuit_breaker.py` (StorageCircuitBreaker), `maintenance.py` (gc/repack/commit-graph), `lfs.py` (git lfs fetch --all), `readme_extractor.py` (`git show HEAD:README*` on the bare clone).
- [ ] Port the corresponding hermetic tests (FakeRunner/Clock/Sleeper injection) under `tests/adapters/git_backup/`.

### Phase 3 — Mirror service + persistence
- [ ] `GitMirrorService` (adapted `engine.py`): preflight storage sentinel check, build `SyncTask` list from (a) GitHub-linked `Repository` rows opted into mirroring and (b) arbitrary URLs from `GitBackupConfig`/`GitMirror`, run the semaphore worker pool, per-target retry → git subprocess → record outcome to `GitMirror` → maintenance → LFS.
- [ ] `GitMirror` repository/port (receives `Database`); failure/state logic reads/writes Postgres instead of JSON files.
- [ ] Credential handling: for github source, decrypt the user's integration token via existing `secret_crypto`; scope it to the git subprocess via env/`GIT_ASKPASS` or a short-lived credential file (deleted after), redacting in logs.

### Phase 4 — Trigger surfaces
- [ ] Taskiq: `app/tasks/git_backup_sync.py` (`@broker.task("ratatoskr.git_backup.sync")`, Redis distributed lock, calls `GitMirrorService`); runtime bundle in `app/di/tasks.py` + bridge in `app/tasks/deps.py`; register `ScheduledTask` (cron `cfg.git_backup.sync_cron`, gated by `cfg.git_backup.enabled`) in `app/tasks/scheduler.py`.
- [ ] Telegram: `app/adapters/telegram/command_handlers/git_mirror_handler.py` — `/mirror <url|owner/name>` (enqueue a one-off mirror), `/mirrors` (status list). Wire in `app/di/telegram_commands.py` (`post_summarize_text` slot). Note: `/backup` already exists for pg backups — use `/mirror` to avoid collision.
- [ ] REST: `/v1/git-mirrors` router — `GET` (list+status), `POST` (register/trigger by URL or repository_id, 202), `GET /{id}`, `DELETE /{id}`. Regenerate OpenAPI (`make generate-openapi`); CI drift gate.

### Phase 5 — Search wiring
- [ ] GitHub-linked mirrors: already covered by the existing `Repository` → `RepositoryEmbeddingGenerator` → Qdrant path; no new vector code.
- [ ] Arbitrary-URL mirrors (no `Repository` row): extract README from the bare clone via `readme_extractor`, then either (decision in this phase) embed under a new vectorized entity type (new `point_ids` helper + `VectorIndexedEntityAdapter` registered in `VectorIndexReconciler`) OR create lightweight synthetic repo records. Default lean: defer arbitrary-URL search to a follow-up; ship mirroring first.

### Phase 6 — Docs, verification, cleanup
- [x] Document all `GIT_BACKUP_*` vars in `docs/reference/environment-variables.md`; add a subsystem note in `CLAUDE.md` + `AGENTS.md`; explanation doc `docs/explanation/git-mirroring.md`; nav entries in `docs/SPEC.md` + CLAUDE.md docs index.
- [x] Quality gates: ruff (clean), mypy (clean on new modules), 125 hermetic tests pass, OpenAPI generate + drift PASS, full app import smoke. (Docker build smoke deferred — image not built locally.)
- [ ] On close: delete this task note (git history is the audit trail).

## Acceptance criteria

- [ ] A scheduled Taskiq job mirrors eligible GitHub-linked repos and configured arbitrary git URLs to `data_path` as bare mirrors, idempotently (re-runs do `remote update`, not re-clone), under a Redis lock.
- [ ] `/mirror <target>` triggers a one-off mirror and `/mirrors` reports per-target status; `/v1/git-mirrors` exposes list/trigger/status/delete and the OpenAPI spec matches (drift gate green).
- [ ] Full resilience behavior preserved: categorized retry, storage circuit breaker, cross-run failure tracking with auto-skip cooldown, large-repo timeout multiplier, post-clone maintenance (gc/repack/commit-graph), Git LFS fetch.
- [ ] No gitout Qdrant/Gemini/TOML/CLI/cron code is carried in; ratatoskr's existing infra is reused.
- [ ] Runtime image has `git` + `git-lfs`; mirror storage is a persistent volume.
- [ ] `make lint`, `make type`, and `pytest` pass; new engine code is covered by ported hermetic tests.
