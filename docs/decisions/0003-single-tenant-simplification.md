# 0003 Single-Tenant Simplification

## Status

Draft, 2026-06

## Context

Ratatoskr is a self-hosted, single-tenant Telegram bot. Access is controlled exclusively by `ALLOWED_USER_IDS`, a comma-separated whitelist of Telegram user IDs set at deploy time. There is exactly one owner per deployment; there are no sign-up flows, no org hierarchies, and no concept of user isolation between distinct human operators.

Despite this architectural reality, the current codebase contains 197 API endpoints, 71 SQLAlchemy model classes, and roughly 34 repository classes, many of which carry multi-tenant scaffolding: per-user ID scoping on every read path, token-family rotation designed to protect shared infrastructure from cross-tenant token replay, a full collaborative-collections ACL layer (collaborators, invites, roles), a device registry originally intended for multi-device push routing, and a sync-v2 protocol with session management, ETag delta negotiation, and conflict detection. These patterns were added incrementally as the product surface grew and as the codebase anticipated eventual multi-tenant hosting that has not materialized.

The total distinct API endpoints called by known clients (browser extension + CLI) is approximately 29, meaning roughly 85% of the API surface is not exercised by any shipping client. The KMP mobile client does consume a broader slice via the sync protocol and collections, but the collaboration-specific endpoints (sharing, ACL, invites) have no known callers.

This document catalogues the multi-tenant complexity, identifies safe removal targets, and defers anything requiring DB migrations or API contract changes.

## Current Complexity

The dominant source of multi-tenant complexity is `user_id` propagation. Every HTTP handler extracts `user["user_id"]` from the JWT dependency, threads it through the service layer, and passes it as a WHERE predicate to every repository read. In a single-owner deployment this filter is always `WHERE user_id = <same constant>` — it never selects a non-empty subset of rows; it is structural dead weight.

A second cluster is the auth layer. The `/auth/refresh` endpoint implements full RFC 6819 token-family rotation: it tracks parent-child token chains, detects retired-token replay, and bulk-revokes entire families on suspected theft. This is the correct design for a shared SaaS where one user's compromised token must not escalate to another user's data. For a single-owner bot behind a private whitelist, the threat model does not include cross-user contamination; replay detection still has value, but the full family-revocation machinery adds ~300 lines of policy logic that fires on a threat that cannot occur.

The collections subsystem carries a full ACL layer: `CollectionCollaborator` and `CollectionInvite` tables, a `GET /{id}/acl` endpoint, share/invite/accept/revoke endpoints, and role-based ownership checks inside the repository. None of these endpoints are called by any known client. The underlying feature — organizing summaries into named groups — is genuinely useful; the sharing infrastructure around it is not.

Sync v2 adds session management, bounded pagination, ETag-based 304 negotiation, and a conflict-resolution path via `POST /sync/apply`. The 304 path is the only piece with a direct single-tenant benefit (bandwidth reduction on unchanged data). The session-creation and conflict-detection paths exist to coordinate multiple concurrent users writing to shared state — a condition that cannot arise with one owner.

The `UserDevice` / device-registry table exists to route push notifications to the correct device among many devices belonging to many users. In a single-owner deployment all registered devices belong to the same person; the per-`(user_id, platform)` index is a no-op discriminator.

Estimated removable lines across all `user_id` filter sites flagged as safe to drop (no schema migration required): approximately 760 lines across `user_content_repository.py`, `summary_repository.py`, `rss_feed_repository.py`, and `request_repository.py`.

## Feature Analysis Table

| Feature | Current LOC (est.) | What multi-tenant adds | Simplified version | Lines saved (est.) | Action |
|---|---|---|---|---|---|
| `user_id` repo filters (safe repos) | ~760 | Isolates rows between tenants | Remove WHERE clauses in the 4 repos flagged `could_drop_filter: true` | ~760 | **Safe — do now** (no schema change, no API change) |
| Refresh token families | ~300 | Cross-user family revocation on replay attack | Keep replay detection; remove `family_id` / `parent_token_hash` chain logic and bulk-revoke-family code path | ~200 | Deferred (requires schema migration to drop `family_id`, `parent_token_hash` columns) |
| Collections collaborators | ~400 | Sharing, roles, ACL, invite accept flow | Keep CRUD collections; drop `CollectionCollaborator`, `CollectionInvite`, ACL endpoints, invite endpoints | ~350 | Deferred (requires schema migration + API contract change) |
| Sync v2 session management | ~180 | Multi-device conflict coordination | Keep GET /full and GET /delta (ETag); drop POST /sessions and POST /apply | ~100 | Deferred (API contract change; KMP client dependency) |
| Device registry (`UserDevice`) | ~80 | Per-user multi-device push routing | Collapse to a flat token store; remove `user_id` FK and `(user_id, platform)` index | ~40 | **Safe — do now** (registry still works; index removal is additive-safe) |
| `logout-all` family enumeration | ~60 | Revoke each family independently with per-family audit entries | Single bulk revoke query on `user_id` | ~40 | Deferred (depends on family removal above) |
| Social auth per-user OAuth state | ~50 | Isolates OAuth state between tenants | Retain; schema constraint is load-bearing for upsert conflict target | 0 | Keep as-is |
| Signal source composite indexes | ~30 | Multi-tenant unique constraints on `(user_id, source_id)` etc. | Retain; indexes are structurally embedded in upsert conflict targets | 0 | Keep as-is |

## Hard Boundaries (Must Keep)

**JWT authentication for the Mobile API.** The KMP client and web frontend authenticate every request with a short-lived access token and a refresh token. Even in a single-owner deployment the JWT layer provides session revocation (log out from one device without invalidating others) and client-ID discrimination (bot vs. mobile vs. web behaviour). Removing it would require replacing the entire auth flow in both clients.

**Browser extension and CLI session tokens.** The CLI uses `/v1/auth/secret-login` and long-lived secret keys. The browser extension depends on cookie-based sessions. Both are active clients with no alternative auth path.

**`client_id` scoping inside `auth_repository`.** `RefreshToken.client_id` and `ClientSecret.user_id` disambiguate sessions across device types. In a single-owner multi-device household (phone + tablet + desktop) the owner still has multiple concurrent sessions that must be independently revocable. `auth_repository` is correctly flagged `could_drop_filter: false`.

**Replay detection inside token refresh.** The `is_revoked` check and the `REJECT` branch of `TokenFamilyPolicy.decide()` guard against an attacker replaying a stolen refresh token. This threat exists even in single-owner deployments (device theft, token leak). The `REVOKE_FAMILY` / bulk-revoke branch can be deferred, but the per-token revocation check must stay.

**Collection CRUD (not collaboration).** Collections are used by the KMP client and are part of the sync contract. The list/create/read/update/delete endpoints and item management must remain. Only the collaboration surface (ACL, share, invite) is safe to defer.

**Health, admin, and observability endpoints.** Operational visibility is not multi-tenancy — these serve the single operator's own deployment monitoring.

**Correlation IDs and full DB persistence of LLM calls, crawl results, and Telegram messages.** These are observability primitives, not multi-tenant features.

## Top-2 Safe Simplifications

### 1. `tools/count_api_endpoints.py` — API surface visibility (IMPLEMENTED)

A stdlib-only script that walks `app/api/routers/` via AST, counts all `@router.{get,post,put,delete,patch}` decorator calls, and prints `API surface: N endpoints (single-tenant budget)` with a per-file breakdown. A CI step (continue-on-error: true) in the `test` job prints the count on every run so future endpoint growth is visible in logs without blocking CI.

**Savings:** no production LOC removed; establishes a measurable complexity budget that makes future drift visible.

### 2. `user_id` WHERE filters — DEFERRED (security hold)

The repository audit originally identified three methods in `user_content_repository.py` (`async_list_goals`, `async_list_custom_digests`, `async_list_highlights`) as safe candidates for `user_id` filter removal: in a single-owner deployment the predicate is always `WHERE user_id = <same constant>` and never selects a non-empty subset of rows.

**Why deferred:** Automated security review correctly flagged the removal as an IDOR risk. Even in a single-owner deployment, removing `user_id` from a WHERE clause creates a forward-looking vulnerability: if a second user ever authenticates (JWT secret compromise, manual DB row addition, or future multi-tenancy reintroduction), the unguarded queries would silently return all rows regardless of caller identity. Defense-in-depth is worth more than 13 lines of savings. The `user_id` filters have been restored with the original semantics.

**Path forward:** This simplification is correct IF it is paired with a schema constraint (CHECK constraint or row-level security policy) that enforces single-tenancy at the DB layer rather than relying on application-level filtering. That is a schema migration that belongs in the deferred batch below.

### 2. Remove the `(user_id, platform)` composite index from `UserDevice` and decouple device registration from user identity

The `UserDevice` table has a composite index on `(user_id, platform)` whose purpose is to efficiently list all devices for a given user on a given platform — a query pattern that only matters when multiple users share the same deployment. The `token` column already carries a UNIQUE constraint that is sufficient for single-owner deduplication.

**What to change:** Drop the `CREATE INDEX` for `(user_id, platform)` from the model definition (SQLAlchemy `Index` constructor in `app/db/models/core.py`) and from the next Alembic migration. The `user_id` FK column on `UserDevice` can remain for now (deferring the full removal to the migration batch). The `POST /v1/notifications/device` handler currently stores `user["user_id"]` on the row; keep writing it but stop relying on the composite index for any query. Any service-layer method that queries `UserDevice` by `(user_id, platform)` should be rewritten to query by `token` (the unique key) or drop the `user_id` predicate from the WHERE clause.

**Why it is safe:** The `token` UNIQUE index is the operationally correct deduplication key — a push token is globally unique regardless of which user registered it. In a single-owner deployment there is only one user, so the `(user_id, platform)` index is never a discriminator; it is pure index maintenance overhead on every insert and update to the table. Dropping an index is a non-destructive schema operation: no data is lost, no query returns wrong results (the planner falls back to the token unique index or a seq scan on a small table), and no API contract changes. The migration that drops the index is a single `DROP INDEX` statement with no data transformation.

**Estimated savings:** ~40 lines in the model definition, any service methods that filter by `(user_id, platform)`, and the index maintenance cost at runtime.

## Client API Coverage Gap

| Client | Endpoints used | Total surface | Coverage % |
|---|---|---|---|
| Browser extension | 2 (`/v1/tags`, `/v1/quick-save`) | 197 | ~1% |
| CLI | 27 | 197 | ~14% |
| Both combined (distinct) | 29 | 197 | ~15% |
| Uncovered surface | 168 | 197 | ~85% |

Note: the KMP mobile client is not included in the client audit data above. It consumes the sync, collections, auth, and content endpoints via the generated OpenAPI client and accounts for a meaningful but unmeasured slice of the remaining 85%. Even accounting for the KMP client, the collaboration endpoints (ACL, share, invite), webhook management, rule automation, and several system/admin endpoints are unlikely to have any active caller.

## Deferred Work

The following simplifications are valid in principle but require DB migrations, API contract changes, or both. They are safe to defer until a dedicated schema-cleanup milestone.

**Token-family rotation removal.** Dropping `RefreshToken.family_id` and `parent_token_hash` columns and simplifying `TokenFamilyPolicy` to per-token revocation only. Requires an Alembic migration to drop the columns and a coordinated update to any client that reads family metadata from the refresh response (currently none known, but the API shape changes). Estimated savings ~200 lines.

**CollectionCollaborator and CollectionInvite removal.** Dropping the two tables, their FK constraints, and the 10 collaboration endpoints (`/acl`, `/share`, `/invite`, etc.) from the collections router. Requires schema migration and an OpenAPI contract bump. The KMP client regenerates from the spec; any generated client code referencing the dropped endpoints will fail to compile, making the removal self-enforcing. Estimated savings ~350 lines.

**Sync v2 session and apply endpoint removal.** Dropping `POST /sync/sessions` and `POST /sync/apply`. The ETag delta path (`GET /delta` with `If-None-Match`) provides the single-tenant bandwidth benefit and should be retained. The session-creation and conflict-apply machinery is the multi-tenant coordination layer. Requires KMP client update to remove calls to the dropped endpoints. Estimated savings ~100 lines.

**`user_id` column removal from structurally constrained repos.** The four repos flagged `could_drop_filter: false` (`signal_source_repository`, `collection_repository`, `social_connection_repository`, `auth_repository`) have `user_id` embedded in unique constraint definitions or upsert conflict targets. Removing the column requires schema migrations that touch composite indexes and potentially change upsert semantics. Safe to batch with the token-family and collaborator migrations above.

**`logout-all` family enumeration simplification.** Currently enumerates all active `family_id` values for the user and revokes each family individually with per-family audit log rows. Once `family_id` is dropped this collapses to a single `UPDATE ... WHERE user_id = :user_id AND is_revoked = false`. Blocked on token-family removal above.
