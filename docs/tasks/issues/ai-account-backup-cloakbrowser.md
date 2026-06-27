---
title: Back up ChatGPT and Claude accounts via CloakBrowser authenticated sessions
status: doing
area: ops
priority: medium
owner: Backend
blocks: []
blocked_by: []
created: 2026-06-27
updated: 2026-06-27
---

- [ ] #task Back up ChatGPT and Claude accounts via CloakBrowser authenticated sessions #repo/ratatoskr #area/ops #status/doing 🔼

## Summary

Add a self-hosted, single-tenant backup subsystem that holds an authenticated session for the operator's own ChatGPT (`chatgpt.com`) and Claude (`claude.ai`) web accounts and periodically mirrors conversations, Projects, project-knowledge files, attachments, and Claude Artifacts to disk. It reuses the existing CloakBrowser stealth sidecar (currently stateless) by adding a persistent authenticated context, and follows the `git_backup` subsystem as its structural template. Design doc: [`docs/explanation/ai-account-backup.md`](../../explanation/ai-account-backup.md).

## Context and decisions

This was scoped after a research pass into ready-made references. Findings that shaped the design:

- There is no single drop-in tool; it is assemble-from-parts. Official ZIP exports are ToS-sanctioned but drop ChatGPT Project grouping, Claude Projects/Artifacts, and uploaded binaries — exactly what this subsystem targets.
- The stealth-scrape path **violates both providers' ToS**; Anthropic demonstrated zero-warning suspension for this class in April 2026. Subsystem ships off by default, double-gated, single-tenant, own-account-only, with conservative cadence and halt-on-expiry.
- CloakBrowser is already integrated (`cloakserve` CDP sidecar) but used statelessly — net-new persistent-session work is required.

Operator decisions recorded:

- **Auth bootstrap:** Mode A (operator-supplied `storage_state` blob; no credential storage). Modes B (noVNC) and C (headless credential login) are out of scope for the initial build.
- **Providers:** ChatGPT and Claude built in parallel on shared scaffolding.
- **Claude plan:** consumer (Pro/Max) — stealth path for Claude; leave an `AI_BACKUP_CLAUDE_COMPLIANCE_KEY` flag to switch to the sanctioned Compliance API if the account becomes Enterprise.

## Key design points

- Internal-API GETs issued **inside the authenticated CloakBrowser page context** (`page.context.request.get`) to keep `cf_clearance` valid against Cloudflare's TLS/IP binding — not a separate httpx client.
- Reuse `UserBrowserSession` (Fernet `encrypted_cookies`) for the `storage_state` blob — zero migration; add `chatgpt.com` / `claude.ai` as `domain` values.
- New `ai_account_backups` lifecycle table (one row per `user_id` + `service`), modeled on `GitMirror`; `WHERE user_id =` IDOR guard on every query.
- Crypto reuses `GITHUB_TOKEN_ENCRYPTION_KEY` via `app.security.secret_crypto`.

## Acceptance criteria

- [ ] `AiBackupConfig` (`app/config/ai_backup.py`) wired into `AppConfig`; `AI_BACKUP_ENABLED` defaults false; data-path traversal validator present.
- [ ] `AiAccountBackup` model + enums + Alembic migration; registered in `ALL_MODELS`; repository methods all carry the `user_id` filter.
- [ ] `CloakBrowserProvider.authenticated_context()` loads/persists encrypted `storage_state`, keeps the SSRF guard, pins the per-domain fingerprint.
- [ ] `ChatGPTBackupClient` walks conversations + gizmo Projects + file downloads; `ClaudeBackupClient` walks conversations + projects + artifacts.
- [ ] Taskiq task `ratatoskr.ai_backup.sync` with Redis lock; scheduler emits it only when `cfg.ai_backup.enabled`.
- [x] Mode A session ingest via `POST /v1/ai-backups/{service}/session` (REST/HTTPS only — Telegram ingest removed in security review: live cookies must not transit non-E2E chat); OpenAPI regenerated.
- [ ] `/ai_backup` and `/ai_backups` status surfaces; `auth_expired` detection halts the service and notifies the operator.
- [ ] On-disk layout with idempotent-by-id writes, `manifest.json`, and `AI_BACKUP_INCREMENTAL` skipping.
- [ ] Host allowlist enforced on every internal-API URL; session secrets redacted from logs and never written to the backup tree.
- [ ] Docs: env-reference rows for `AI_BACKUP_*`; CLAUDE.md Agent Map + Environment Variables rows; design doc kept in sync.
- [ ] Unit tests (config validators, repository IDOR/lifecycle, client pagination/tree-walk against fixtures, allowlist rejection, auth-expiry classification); E2E-gated integration test for the authenticated-context round-trip.

## Subtasks (phased)

- [x] P0 — schema + config + repository + task skeleton + scheduler block + REST/Telegram stubs. **Landed:** `AiBackupConfig` wired into `AppConfig`; `AiAccountBackup` model + enums + migration `0049` + `ALL_MODELS`; `AiBackupRepository` (IDOR-guarded); `ratatoskr.ai_backup.sync` Taskiq task (Redis-locked, no scraping) + scheduler block + worker/requeue registration; read-only `/v1/ai-backups` REST (OpenAPI regenerated) + `/ai_backup` `/ai_backups` Telegram. Verified: ruff + mypy clean, 15 new tests + 40 existing scheduler/config tests pass. REST session-ingest/trigger and the scrape clients are deferred to P1.
- [x] P1/P2 — `authenticated_context()` + both backup clients on shared scaffolding + Mode A ingest + on-disk writer + incremental. **Landed:** `allowlist`/`session_store`/`disk_writer`/`client_factory`/`service` + `content/browser_auth/authenticated_context` (AuthedFetcher + PlaywrightAuthedFetcher) + `chatgpt_client`/`claude_client`; task wired to the orchestrator (tasks-layer notifier for Telegram/healthcheck, keeping adapters off the di/tasks tiers); REST `POST /v1/ai-backups/{service}/session` + Telegram `/ai_backup_login`. Verified: ruff + mypy clean, 78 new unit tests (fakes/fixtures) pass, OpenAPI regenerated. **Not validated live** — clients carry `TODO(live-validation)`; Claude Compliance-API path and project-knowledge-file downloads deferred.
- [x] P3 — auth-expiry/notify, Healthchecks, rate-cap + jitter, docs, OpenAPI regen, tests. **Landed:** auth-expiry halt+notify, Healthchecks pings, request-cap+jitter, OpenAPI regen, and the unit suite shipped with P1; this phase added the docs (`AI_BACKUP_*` section in `docs/reference/environment-variables.md`; Agent-Map + Database-Models + Env-Variables rows in `CLAUDE.md`), the two remaining spec tests (`tests/api/test_ai_backups_session.py` for the REST session-ingest endpoint; `tests/adapters/content/browser_auth/test_authenticated_context_cm.py` for the context-manager teardown), and an `AI_BACKUP_DATA_PATH` absolute-path config validator. Verified: 102 ai_backup/browser_auth/REST tests pass, ruff + mypy clean, markdownlint clean. The three low-priority items (shared fingerprint-module extraction → `app/adapters/content/scraper/fingerprint.py`; `last_error` URL redaction → `app/adapters/ai_backup/redaction.py`; disk-writer symlink-TOCTOU hardening via `O_NOFOLLOW`) were subsequently implemented (135 + 98 scraper tests pass, ruff + mypy clean). **Live validation against real accounts is still required.**
- [ ] P4 (optional) — Mode B noVNC login; git-versioned backup tree; Claude Compliance-API mode.

## Known gaps (document, do not block)

- ChatGPT Deep Research structured citations and reasoning trace are not in `/backend-api` (only the report text); reachable only via OpenAI's paid developer API.
- ChatGPT Custom GPT system prompts are not confirmed retrievable internally.
