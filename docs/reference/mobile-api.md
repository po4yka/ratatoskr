# Mobile API Specification (Ratatoskr)

- Version: 1.3
- Last Updated: 2026-05-23
- Canonical machine-readable contract: `docs/openapi/mobile_api.yaml` and `docs/openapi/mobile_api.json`

## Overview

This document is a developer-facing summary of the mobile API implemented by the FastAPI app.

- Base API prefix: `/v1`
- Primary clients: mobile apps (Android/iOS/KMP), Telegram Mini App, web interface (`ratatoskr-web` repo)
- Envelope contract: all JSON business responses use `success`, `data`, `meta`, and standardized `error`
- Mixed-source aggregation surface: `/v1/aggregations`
- Phase 3 signal/source triage surface: `/v1/signals`
- Social provider connection surface: `/v1/social/*` for OAuth connect/callback, disconnect, and token-safe connection status
- OpenAPI source of truth: `app.api.main:app`, generated into `docs/openapi/mobile_api.yaml` and `docs/openapi/mobile_api.json` with `make generate-openapi`
- Cross-repo regeneration workflow: [`docs/reference/openapi-contract-workflow.md`](openapi-contract-workflow.md)
- Sync protocol implementation map: [`docs/reference/sync-protocol.md`](sync-protocol.md)

The same FastAPI host also serves the web SPA:

- `/web` and `/web/*` -> SPA index entrypoint
- `/static/web/*` -> built frontend assets

## Base URLs

- Production: `https://ratatoskrapi.po4yka.com`
- Local: `http://localhost:8000`

Examples:

- `GET http://localhost:8000/v1/summaries`
- `GET http://localhost:8000/health`

## Authentication Modes

Most `/v1/*` endpoints require bearer auth:

- Header: `Authorization: Bearer <access_token>`

The web client uses a hybrid auth strategy:

- Telegram WebApp context: `X-Telegram-Init-Data`
- Browser JWT context: `Authorization: Bearer <access_token>` with refresh via `/v1/auth/refresh`

Digest endpoints (`/v1/digest/*`) require Telegram WebApp authentication via initData (validated by backend middleware/dependencies).

### Allowlist semantics (`ALLOWED_USER_IDS`)

All four authentication paths — JWT (`Authorization: Bearer ...`), Telegram WebApp initData, Telegram Login Widget, and secret-key login — fail **closed** when `ALLOWED_USER_IDS` is empty: the request is rejected with `401`/`403`, regardless of token validity. The matching call sites are `app/api/routers/auth/{dependencies,webapp_auth,telegram,secret_auth}.py`. Bot-internal allowlist enforcement (`app/adapters/telegram/access_controller.py`) shares the same `Config.is_user_allowed` helper.

Setting `ALLOWED_USER_IDS=""` in production is supported only as a deliberate "lock everyone out" posture (e.g. during incident response). Multi-user deployments must enumerate every Telegram user_id explicitly.

### Client ID allowlist semantics (`ALLOWED_CLIENT_IDS`)

When `ALLOWED_CLIENT_IDS` is populated, all JWT-issuing and JWT-refresh flows reject unknown `client_id` values with an authorization error. When the list is empty, local/development deployments accept every syntactically valid `client_id` and log a warning.

Production/public deployments (`APP_ENV=production` or `API_PUBLIC_EXPOSURE=true`) fail startup if `ALLOWED_CLIENT_IDS` is empty unless `AUTH_ALLOW_ANY_CLIENT_ID=true` is explicitly set. Use the override only when broad client access is intentional and documented; it does not change token rotation semantics.

### Auth tokens

JWT access and refresh tokens are issued by Ratatoskr with issuer `ratatoskr` and audience `ratatoskr-api`. Token decode requires `exp`, `iat`, `type`, `user_id`, `iss`, and `aud`; tokens with a wrong issuer or audience are rejected. For one rollout only, each freshly started API worker accepts legacy signed tokens that are missing only `aud` and/or `iss` for five minutes, logging `jwt_legacy_missing_aud_iss_accepted`; remove this compatibility window after one release once old mobile/web tokens have naturally expired or refreshed.

### Rate limits

API rate limits run before auth handlers. Auth endpoints use separated buckets so a spray against one login surface does not consume the refresh/session budget for another surface. `POST /v1/auth/credentials-login` uses `credentials_login` (`API_RATE_LIMIT_CREDENTIALS_LOGIN`, default 5/window), `POST /v1/auth/secret-login` uses `secret_login` (`API_RATE_LIMIT_SECRET_LOGIN`, default 10/window), `POST /v1/auth/telegram-login` uses `telegram_login`, `POST /v1/auth/refresh` uses `auth_refresh`, and the remaining auth/session/secret-key/GitHub-auth endpoints have route-specific `auth_*` buckets. For unauthenticated credentials, secret, Telegram, and refresh requests, the rate-limit key is `(client_id, client_ip)` when a valid client ID can be extracted from the JSON body or refresh JWT; otherwise it falls back to `client_ip`.

`request.client.host` is the source IP used by the middleware. This is only as accurate as the ASGI server/proxy chain: behind CGNAT or a reverse proxy, many legitimate clients can share one IP unless the deployment terminates and forwards client identity correctly. Keep Redis-backed rate limiting enabled in production/public deployments so every worker shares counters, and treat `ratatoskr_rate_limit_hits_total{bucket="credentials_login"}` spikes as possible credential stuffing.

## API Surface Freeze Policy

The mobile API surface is contract-locked against the generated `docs/openapi/mobile_api.yaml` / `docs/openapi/mobile_api.json` pair. Changes are gated by:

- **Generated spec drift check** — `tools/scripts/generate_openapi.py --check` fails when committed OpenAPI docs differ from `app.api.main:app`. Run `make generate-openapi` after API model, router, response envelope, or contract-version changes.
- **CI contract tests** — `tests/api/test_openapi_sync.py`, `tests/api/test_openapi_security.py`, `tests/api/test_runtime_openapi_drift.py`, and `tests/tools/test_generate_openapi.py` reject route drift, schema drift, missing security/error declarations, and stale generated docs. The job runs on every PR via the `test` CI job.
- **Contract semver in the envelope** — every success response carries `meta.api_version` (sourced from `app.api.models.responses.common.API_CONTRACT_VERSION`). The constant is independent of `meta.version` (which tracks app/build deploys).

### Agent contract map

| Concern | Backend owner | Downstream owner |
|---|---|---|
| Generated contract | `app.api.main:app`, `app/api/models/`, `app/api/routers/`, `tools/scripts/generate_openapi.py`, `docs/openapi/mobile_api.yaml`, `docs/openapi/mobile_api.json` | `ratatoskr-web/docs/openapi/mobile_api.yaml`, `ratatoskr-web/src/api/generated.ts`, `ratatoskr-client/core/api-generated/src/commonMain/openapi/mobile_api.yaml`, `ratatoskr-client/core/api-generated/src/commonMain/generated/` |
| Auth/session | `app/api/routers/auth/`, `app/api/routers/auth/tokens.py`, `app/api/routers/auth/cookies.py`, `app/infrastructure/persistence/repositories/auth_repository.py`, `app/db/models/core.py::RefreshToken` | Web: `src/auth/AuthProvider.tsx`, `src/auth/storage.ts`, `src/api/client.ts`; KMP: `feature/auth/`, `core/data/src/*/kotlin/.../data/local/`, `core/data/src/commonMain/kotlin/.../data/remote/auth/` |
| Social connections | `app/api/routers/social_auth.py`, `app/api/models/responses/social.py`, `app/application/services/social_auth_service.py`, `app/infrastructure/persistence/repositories/social_connection_repository.py`, `app/db/models/social.py` | Web/KMP clients consume `SocialConnectionsSuccessResponse`; response payloads expose provider status, username, scopes, expirations, timestamps, and no token material |
| Sync v2 | `app/api/routers/sync.py`, `app/api/services/sync/`, `app/infrastructure/persistence/sync_aux_read_adapter.py` | KMP: `feature/sync/`, feature-owned `SyncItemApplier` and `PendingOperationHandler` bindings |
| Streaming request progress | `app/api/routers/streams.py`, `app/adapters/content/streaming/`, `app/adapters/content/graph_url_processor.py` | Web: `src/api/streamRequest.ts`, `src/hooks/useRequestStream.ts`, `src/features/submit/SubmitPage.tsx` |

Do not edit generated OpenAPI files by hand. For backend contract changes, run `make generate-openapi`, `make check-openapi-drift`, `make check-openapi-validate`, and `make check-openapi`; then update web and KMP pins using their repo-local regeneration commands.

### When to bump `api_version`

Bump the major version when **any** of the following changes:

- Removing or renaming a path or HTTP method
- Removing a required field from a response or request
- Tightening a field type (e.g. `string` → `integer`)
- Changing the envelope shape (`success`, `data`, `error`, `meta`)

Bump the minor version when:

- Adding a new path / method
- Adding a non-required response field

Patch is reserved for documentation-only changes (`description`, examples).

### Migration path for clients

Mobile and CLI clients pin against `meta.api_version` and SHOULD refuse to upgrade across major versions automatically. Backend changes that bump the major version MUST be coordinated with the `../ratatoskr-client` KMP team — see `docs/tasks/issues/map-ratatoskr-mobile-api-contract-to-kmp-readiness.md` for the contract map.

## Router and Service Boundaries

FastAPI routers are transport-focused and delegate orchestration to service collaborators:

- `app/api/routers/digest.py` delegates digest workflow construction and trigger queueing through `DigestFacade`.
- `app/api/routers/system.py` delegates DB dump/info/cache orchestration through `SystemMaintenanceService`.
- `app/api/routers/admin.py` delegates owner diagnostics composition to `DiagnosticsService`; `AdminReadService` is intentionally limited to admin read screens such as users, job status, content health, LLM costs, and audit logs.
- System cache clearing uses `RedisCache.clear_prefix("url")` through the service layer rather than direct inline Redis scan/delete in router handlers.

This keeps auth/input/output mapping in routers while DB/Redis/file logic remains in dedicated service classes.

`GET /v1/admin/diagnostics` remains owner-only and returns `DiagnosticsSuccessResponse`. `DiagnosticsService` owns the 30-second process-local cache, redacted health/error details, scraper configuration diagnostics, vector lag from `VectorIndexReconciler`, queue backlog, storage growth, integration-health status, social provider configured/count/reauth/failure summaries, and provider failure summaries.

## Envelope and Error Contract

Success response shape:

```json
{
  "success": true,
  "data": {},
  "meta": {
    "timestamp": "2026-03-06T00:00:00Z",
    "version": "1.0"
  }
}
```

Error response shape:

```json
{
  "success": false,
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Invalid request",
    "details": {},
    "correlation_id": "req_abc123"
  },
  "meta": {
    "timestamp": "2026-03-06T00:00:00Z",
    "version": "1.0"
  }
}
```

GitHub auth endpoints use the same error envelope. Stable GitHub-specific `error.code` values:

- `oauth_state_invalid`: OAuth/device state is invalid for the authenticated user.
- `github_token_exchange_failed`: GitHub OAuth/token exchange or local OAuth dependency failed; retry may be safe depending on `retryable`.
- `github_token_invalid`: PAT/OAuth token is invalid, revoked, expired, missing required scopes, or the integration is absent.
- `github_oauth_rate_limited`: GitHub rate-limited the OAuth/device request; clients should honor `error.retry_after` when present.

## Sync Model (Current)

Current sync uses explicit sessions and chunked/full + delta + apply endpoints:

1. `POST /v1/sync/sessions` to create/resume session
2. `GET /v1/sync/full?session_id=...&limit=...` for initial full data chunks
3. `GET /v1/sync/delta?session_id=...&cursor=...&limit=...` for incremental updates
4. `POST /v1/sync/apply` to send client-side changes back to server

## Streaming Model (SSE)

`GET /v1/requests/{request_id}/stream` returns a Server-Sent Events stream of real-time progress for an in-flight summary. The connection stays open until a terminal `done` or `error` event is emitted, or the client disconnects.

- **Auth**: same bearer JWT chain as the rest of `/v1`. Unauthenticated → `401`. Authenticated but the request is owned by a different user → `403` (collapsed with not-found to prevent request-id enumeration).
- **Heartbeats**: `EventSourceResponse(ping=15)` emits `: keepalive` comment lines every 15s on idle.
- **Events**: `event: phase | section | done | error`. The `data:` field is a JSON object whose schemas (`StreamPhaseEvent`, `StreamSectionEvent`, `StreamDoneEvent`, `StreamErrorEvent`) are documented in `docs/openapi/mobile_api.yaml`.
- **Phase values**: `extracting → summarizing → validating → persisting → done`.
- **Section values**: JSON field names from the summary contract (`summary_250`, `tldr`, `key_ideas`, `topic_tags`).
- **Disconnect-tolerant**: closing the SSE connection does not cancel the underlying summarization; the `Summary` and `LLMCall` rows still persist.
- **Late subscribers**: the `StreamHub` keeps a 64-event ring buffer per request, so a client that subscribes mid-flight replays recent backlog before joining the live tail.
- **Web client**: `ratatoskr-web/src/api/streamRequest.ts` carries the bearer header via `@microsoft/fetch-event-source` and refreshes once on `401`. Polling fallback engages after two consecutive fatal closes — see `ratatoskr-web/src/hooks/useRequestStream.ts`.

Backend toggle: `URL_FLOW_STREAMING_ENABLED` (default `true`).

## Search and Discovery Parameters

`GET /v1/search` supports:

- Required: `q`
- Pagination: `limit`, `offset`
- Ranking mode: `mode=auto|keyword|semantic|hybrid`
- Filters: `language`, `tags[]`, `domains[]`, `start_date`, `end_date`, `is_read`, `is_favorited`
- Threshold: `min_similarity`

`GET /v1/search/semantic` supports:

- Required: `q`
- Pagination: `limit`, `offset`
- Filters: `language`, `tags[]`, `domains[]`, `start_date`, `end_date`, `is_read`, `is_favorited`
- Scope: `user_scope`
- Threshold: `min_similarity`

## Collections Parameters

`GET /v1/collections` supports:

- `parent_id` (for subtree listing)
- `membership=any|owned|shared` (`any` is the backwards-compatible default; `shared` returns only collections where the caller is an active collaborator and not the owner)
- `limit`
- `offset`

`GET /v1/collections/invites/incoming` lists pending collection invites addressed to the caller. It supports `limit` and `offset` and returns the invited collection metadata, granted role, invite token, inviter user ID, creation time, and optional expiration time. `POST /v1/collections/{collection_id}/invite` accepts optional `recipient_user_id` for user-targeted invites; targeted invites can only be accepted by that recipient.

## Endpoint Index

### Platform and Health

- `GET /`
- `GET /web`
- `GET /web/{path:path}`
- `GET /health`
- `GET /health/live`
- `GET /health/ready`
- `GET /health/detailed`
- `GET /metrics`

Health endpoints are a documented probe carve-out from the business-response envelope rule; see `docs/decisions/0019-health-endpoint-envelope-carveout.md`. Successful responses may use the standard success envelope, but infrastructure callers must treat status code and the endpoint-specific probe schema as authoritative. Readiness failure may return a raw object with top-level `ready`, `error`, and `timestamp`.

### Authentication

- `POST /v1/auth/telegram-login`
- `POST /v1/auth/refresh`
- `POST /v1/auth/logout`
- `POST /v1/auth/logout-all`
- `GET /v1/auth/me`
- `DELETE /v1/auth/me`
- `GET /v1/auth/sessions`
- `POST /v1/auth/secret-login`
- `GET /v1/auth/secret-keys`
- `POST /v1/auth/secret-keys`
- `POST /v1/auth/secret-keys/{key_id}/rotate`
- `POST /v1/auth/secret-keys/{key_id}/revoke`
- `POST /v1/auth/credentials-login`
- `POST /v1/auth/credentials/change-password`
- `POST /v1/auth/apple/start`
- `POST /v1/auth/apple/callback`
- `POST /v1/auth/magic-link/request`
- `GET /v1/auth/magic-link/verify`
- `GET /v1/auth/me/telegram`
- `POST /v1/auth/me/telegram/link`
- `POST /v1/auth/me/telegram/complete`
- `DELETE /v1/auth/me/telegram`

The web client uses the credentials flow whenever `CREDENTIALS_LOGIN_PEPPER` is set on the server (pepper presence is the only gate; there is no separate enable flag — without a pepper the route returns `503 Configuration error`):

1. The owner runs `ratatoskr credentials set --user-id <id> --nickname <name> [--email <addr>]` once after deploy to seed the row in `user_credentials`. There is no public signup; this is the only writer.
2. `POST /v1/auth/credentials-login` accepts `{ identifier, password, remember_me, client_id }` where `identifier` is either the nickname or email (case-folded; `@` presence disambiguates). Returns the same `{ tokens, sessionId }` envelope as the other login flows.
3. Remember Me semantics: - `remember_me=true`  → 30-day refresh TTL; web client persists tokens in `localStorage`. - `remember_me=false` → 12-hour refresh TTL (configurable via `CREDENTIALS_LOGIN_NO_REMEMBER_HOURS`); web client persists tokens in `sessionStorage` (vanish on browser close); refresh cookie is issued without `Max-Age` so it is also a session cookie.
4. Anti-enumeration: every failure path (unknown identifier, wrong password, non-allowlisted user, exhausted lockout) returns the same uniform `401 Invalid credentials`; a decoy argon2 verify runs on the no-row-found path to keep wall-clock timing matched. A `Retry-After` header is set only when the row is locked.
5. Lockout follows the secret-login pattern (default 5 failures / 15 minutes) but uses a *separate* counter on `user_credentials.failed_attempts`; locking credentials login does not lock secret-key login and vice versa.
6. `POST /v1/auth/credentials/change-password` requires the current password and is bearer-auth gated. Active sessions are not revoked on rotation (parity with secret-key rotation).

Example `credentials-login` request:

```http
POST /v1/auth/credentials-login
Content-Type: application/json

{
  "identifier": "owner",
  "password": "correct horse battery staple",
  "remember_me": true,
  "client_id": "web-v1"
}
```

External CLI and hosted MCP clients usually use the secret-key flow instead of Telegram login:

1. `POST /v1/auth/secret-keys` creates or registers a client secret. The plaintext secret is returned once, at creation or rotation time only.
2. `POST /v1/auth/secret-login` exchanges that secret for an access token, refresh token, and session ID.
3. `POST /v1/auth/refresh` rotates the refresh token and returns a fresh access token.
4. `POST /v1/auth/logout` revokes the supplied refresh token or the refresh cookie-backed session.
5. `POST /v1/auth/logout-all` (bearer-auth required) revokes every active refresh-token family for the current user across all devices and writes one `AuditLog` row per revoked family. Returns `{ revokedFamilies, revokedTokens }` inside the standard envelope. Use this for the lost-device flow.
6. `POST /v1/auth/secret-keys/{key_id}/rotate` replaces the old plaintext secret for future logins.
7. `POST /v1/auth/secret-keys/{key_id}/revoke` is idempotent and prevents future `secret-login` exchanges with that secret.

Refresh-token family rotation (since `0016_add_refresh_token_family_columns`):

- Every refresh token belongs to a family identified by `family_id`. The first token of a session is the root (no `parent_token_hash`); each rotation issues a child carrying the predecessor's `family_id` and `parent_token_hash = sha256(presented_token)`.
- Presenting a retired (already-rotated) token is replay evidence: the `/refresh` endpoint revokes the entire matching family — not every active session — so a stolen mobile token does not log the user out on their desktop. One `AuditLog` row (`event = refresh_family_revoked`, `reason = retired_token_replay`) is written per cascade.
- Presenting an expired token (but not revoked) returns `401` without cascading — benign client mistake.

Apple Sign-In and magic-link email are optional identity providers that reuse the same JWT access/refresh token envelope as credentials, Telegram, and secret-key login. They do not create public accounts: the provider identity must resolve to an existing allowed Ratatoskr user by a linked `user_identities` row or by a known email on `user_credentials`; successful first login upserts the provider identity so later logins do not duplicate the account. Apple requires `APPLE_SIGNIN_CLIENT_ID` and validates `id_token` against Apple's JWKS with issuer/audience/nonce checks. Magic-link requires a known email and sends a one-time 15-minute token through the configured email sink; `EMAIL_PROVIDER=none` returns the generated link in the response for local development only.

Example `secret-login` request:

```http
POST /v1/auth/secret-login
Content-Type: application/json

{
  "user_id": 123456,
  "client_id": "cli-workstation-v1",
  "secret": "paste-once-secret"
}
```

Example `secret-login` success payload:

```json
{
  "success": true,
  "data": {
    "tokens": {
      "accessToken": "eyJ...",
      "refreshToken": "eyJ...",
      "expiresIn": 3600,
      "tokenType": "Bearer"
    },
    "user": {
      "userId": 123456,
      "username": "reader",
      "clientId": "cli-workstation-v1",
      "isOwner": false,
      "createdAt": "2026-04-12T09:30:00Z"
    },
    "sessionId": 88
  }
}
```

### Summaries and Articles

Canonical summary endpoints:

- `GET /v1/summaries`
- `GET /v1/summaries/by-url`
- `GET /v1/summaries/{summary_id}`
- `GET /v1/summaries/{summary_id}/related`
- `PATCH /v1/summaries/{summary_id}`
- `DELETE /v1/summaries/{summary_id}`
- `POST /v1/summaries/bulk/mark-read`
- `POST /v1/summaries/bulk/favorite`
- `POST /v1/summaries/bulk/delete`
- `GET /v1/summaries/{summary_id}/content`
- `GET /v1/summaries/{summary_id}/export`
- `POST /v1/summaries/{summary_id}/favorite`
- `POST /v1/summaries/{summary_id}/feedback`
- `PATCH /v1/summaries/{summary_id}/reading-position`
- `POST /v1/summaries/{summary_id}/tags`
- `DELETE /v1/summaries/{summary_id}/tags/{tag_id}`
- `GET /v1/summaries/{summary_id}/highlights`
- `POST /v1/summaries/{summary_id}/highlights`
- `PATCH /v1/summaries/{summary_id}/highlights/{highlight_id}`
- `DELETE /v1/summaries/{summary_id}/highlights/{highlight_id}`
- `POST /v1/summaries/{summary_id}/audio`
- `GET /v1/summaries/{summary_id}/audio`

Alias endpoints for compatibility (`/v1/articles/*`) map to the same handlers:

- `GET /v1/articles`
- `GET /v1/articles/by-url`
- `GET /v1/articles/{summary_id}`
- `GET /v1/articles/{summary_id}/related`
- `PATCH /v1/articles/{summary_id}`
- `DELETE /v1/articles/{summary_id}`
- `POST /v1/articles/bulk/mark-read`
- `POST /v1/articles/bulk/favorite`
- `POST /v1/articles/bulk/delete`
- `GET /v1/articles/{summary_id}/content`
- `GET /v1/articles/{summary_id}/export`
- `POST /v1/articles/{summary_id}/favorite`
- `POST /v1/articles/{summary_id}/feedback`
- `PATCH /v1/articles/{summary_id}/reading-position`
- `POST /v1/articles/{summary_id}/tags`
- `DELETE /v1/articles/{summary_id}/tags/{tag_id}`
- `GET /v1/articles/{summary_id}/highlights`
- `POST /v1/articles/{summary_id}/highlights`
- `PATCH /v1/articles/{summary_id}/highlights/{highlight_id}`
- `DELETE /v1/articles/{summary_id}/highlights/{highlight_id}`
- `POST /v1/articles/{summary_id}/audio`
- `GET /v1/articles/{summary_id}/audio`

`GET /v1/summaries/{summary_id}/related` and its `/v1/articles/{summary_id}/related` alias return vector-similar summaries for the authenticated user's summary. The endpoint uses the same related-reads application service as Telegram follow-up messages, excludes the source request from results, and returns `404` when the summary does not belong to the caller.

Bulk operations require bearer authentication and use the standard success/error envelope. `POST /v1/summaries/bulk/mark-read` and its `/v1/articles/bulk/mark-read` alias accept:

```json
{
  "summary_ids": [101, 102]
}
```

`POST /v1/summaries/bulk/favorite` and `/v1/articles/bulk/favorite` accept:

```json
{
  "summary_ids": [101, 102],
  "value": true
}
```

`POST /v1/summaries/bulk/delete` and `/v1/articles/bulk/delete` accept:

```json
{
  "summary_ids": [101, 102]
}
```

All three bulk operations accept up to 500 IDs and silently skip IDs outside the authenticated user's scope. The response shape is:

```json
{
  "success": true,
  "data": {
    "updated": 2
  },
  "meta": {
    "api_version": "1.0"
  }
}
```

### Requests and Processing

- `POST /v1/requests`
- `GET /v1/requests/{request_id}`
- `GET /v1/requests/{request_id}/status`
- `POST /v1/requests/{request_id}/retry`
- `GET /v1/urls/check-duplicate`

### Import and Export

- `GET /v1/import`
- `POST /v1/import`
- `GET /v1/import/{job_id}`
- `DELETE /v1/import/{job_id}`
- `GET /v1/export`

Import endpoints require bearer authentication. `GET /v1/import` lists the authenticated user's import jobs so clients can recover job status after restart or reconnect:

```http
GET /v1/import
Authorization: Bearer eyJ...
```

Response shape:

```json
{
  "success": true,
  "data": {
    "jobs": [
      {
        "id": 42,
        "status": "completed",
        "progress": {
          "totalItems": 10,
          "processedItems": 10,
          "successfulCount": 9,
          "failedCount": 1,
          "duplicateCount": 0,
          "completionPercent": 100
        }
      }
    ]
  },
  "meta": {
    "api_version": "1.0"
  }
}
```

### Aggregations

- `POST /v1/aggregations`
- `GET /v1/aggregations`
- `GET /v1/aggregations/{session_id}`
- `DELETE /v1/aggregations/{session_id}`

`POST /v1/aggregations` accepts a bundle of 1-25 URL items:

- `type`: currently `url`
- `url`: `http://` or `https://` source URL
- `source_kind_hint`: optional classification hint. Allowed values are `x_post`, `x_article`, `threads_post`, `instagram_post`, `instagram_carousel`, `instagram_reel`, `web_article`, `telegram_post`, and `youtube_video`.
- `metadata`: optional per-item metadata

Bundle-level fields:

- `lang_preference`: `auto`, `en`, or `ru`
- `metadata`: optional request metadata attached to the aggregation session

`POST /v1/aggregations` is the canonical execution entrypoint and is currently **blocking**: the request waits for extraction plus synthesis and returns a terminal session snapshot on success. Use `GET /v1/aggregations/{session_id}` and `GET /v1/aggregations` to revisit stored runs later or recover after a client/network interruption.

The create response returns:

- `session`: create-specific session view with camelCase lifecycle/progress fields
- `aggregation`: synthesized bundle output
- `items`: per-item extraction status, request IDs, and item-level failures

Persisted aggregation sessions expose these statuses:

- `pending`
- `processing`
- `completed`
- `partial`
- `failed`

Key progress fields:

- `progress.totalItems`
- `progress.processedItems`
- `progress.successfulCount`
- `progress.failedCount`
- `progress.duplicateCount`
- `progress.completionPercent`
- `queuedAt`, `startedAt`, `completedAt`, `lastProgressAt`

Example create request:

```http
POST /v1/aggregations
Authorization: Bearer eyJ...
Content-Type: application/json

{
  "items": [
    {
      "type": "url",
      "url": "https://x.com/example/status/1",
      "source_kind_hint": "x_post"
    },
    {
      "type": "url",
      "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
      "source_kind_hint": "youtube_video"
    }
  ],
  "lang_preference": "en",
  "metadata": {
    "submitted_by": "cli"
  }
}
```

Example create response:

```json
{
  "success": true,
  "data": {
    "session": {
      "sessionId": 42,
      "correlationId": "cid-agg-42",
      "status": "completed",
      "sourceType": "mixed",
      "successfulCount": 2,
      "failedCount": 0,
      "duplicateCount": 0,
      "queuedAt": "2026-04-12T09:31:00Z",
      "startedAt": "2026-04-12T09:31:01Z",
      "completedAt": "2026-04-12T09:31:07Z",
      "lastProgressAt": "2026-04-12T09:31:07Z",
      "progress": {
        "totalItems": 2,
        "processedItems": 2,
        "successfulCount": 2,
        "failedCount": 0,
        "duplicateCount": 0,
        "completionPercent": 100
      },
      "failure": null
    },
    "aggregation": {
      "session_id": 42,
      "status": "completed",
      "source_type": "mixed"
    },
    "items": [
      {
        "position": 0,
        "sourceKind": "x_post",
        "status": "extracted",
        "requestId": 501,
        "failure": null
      },
      {
        "position": 1,
        "sourceKind": "youtube_video",
        "status": "extracted",
        "requestId": 502,
        "failure": null
      }
    ]
  }
}
```

Example get request:

```http
GET /v1/aggregations/42
Authorization: Bearer eyJ...
```

Example list request:

```http
GET /v1/aggregations?limit=20&offset=0&status=processing
Authorization: Bearer eyJ...
```

`GET /v1/aggregations/{session_id}` and `GET /v1/aggregations` return persisted session records using the repository-backed snake_case fields (`id`, `correlation_id`, `successful_count`, `queued_at`, and so on) plus derived `progress` and `failure` objects.

Example list response:

```json
{
  "success": true,
  "data": {
    "sessions": [
      {
        "id": 42,
        "user": 123456,
        "correlation_id": "cid-agg-42",
        "total_items": 2,
        "successful_count": 2,
        "failed_count": 0,
        "duplicate_count": 0,
        "status": "completed",
        "processing_time_ms": 6123,
        "queued_at": "2026-04-12T09:31:00Z",
        "started_at": "2026-04-12T09:31:01Z",
        "completed_at": "2026-04-12T09:31:07Z",
        "last_progress_at": "2026-04-12T09:31:07Z",
        "created_at": "2026-04-12T09:31:00Z",
        "updated_at": "2026-04-12T09:31:07Z",
        "progress": {
          "totalItems": 2,
          "processedItems": 2,
          "successfulCount": 2,
          "failedCount": 0,
          "duplicateCount": 0,
          "completionPercent": 100
        },
        "failure": null
      }
    ]
  },
  "meta": {
    "pagination": {
      "total": 1,
      "limit": 20,
      "offset": 0,
      "hasMore": false
    }
  }
}
```

Duplicate handling and retries:

- duplicate source items are accepted; later duplicates are persisted with duplicate status and counted in `duplicateCount` / `duplicate_count`
- duplicate bundles are **not** de-duplicated at the bundle level; retrying the same bundle creates a new session
- `DELETE /v1/aggregations/{session_id}` permanently removes an owned aggregation session and its child items; missing or unowned sessions return `404`
- there is currently no public cancel endpoint; in-flight sessions cannot be stopped through the mobile API
- retry clients should retry the same request only after transport failures or a `PROCESSING_ERROR`; successful create calls already persisted the bundle session

Common pre-execution failures:

- rollout denied because aggregation is disabled or the user is not in the current rollout stage
- validation failure because the bundle is malformed or exceeds limits
- unsupported or blocked URLs, including localhost/private-network SSRF targets
- rate limiting on aggregation create per user or per client ID

Execution failures:

- `500 PROCESSING_ERROR` with `details.reason_code=AGGREGATION_TIMEOUT` when server-side aggregation exceeds the processing window
- `500 PROCESSING_ERROR` with `details.reason_code=AGGREGATION_UPSTREAM_FAILURE` when no source extraction or synthesis output could be completed successfully

### Signal Scoring and Sources

- `GET /v1/signals`
- `GET /v1/signals/health`
- `GET /v1/signals/sources/health`
- `POST /v1/signals/sources/{source_id}/active`
- `POST /v1/signals/{signal_id}/feedback`
- `POST /v1/signals/topics`

`GET /v1/signals` returns the authenticated user's ranked signal queue. Signal rows include scoring/status fields plus source and topic context such as `final_score`, `filter_stage`, `feed_item_title`, `feed_item_url`, `source_kind`, `source_title`, and `topic_name`.

`GET /v1/signals/health` returns Phase 3 readiness for signal scoring:

- `vector.ready`: whether the vector store health check currently passes
- `vector.required`: whether vector search is required by runtime config
- `vector.collection`: active vector collection name when available
- `sources.total`, `sources.active`, and `sources.errored`: source health counts visible to the user

`GET /v1/signals/sources/health` returns per-source rows for the authenticated user's subscriptions. Rows include source identity, active state, fetch error counts, last fetch/success timestamps, subscription active state, cadence, and next fetch time.

`POST /v1/signals/sources/{source_id}/active` enables or pauses an existing subscribed source:

```json
{
  "is_active": false
}
```

`POST /v1/signals/{signal_id}/feedback` records one user action. Allowed `action` values are `like`, `dislike`, `skip`, `hide_source`, `queue`, and `boost_topic`.

`POST /v1/signals/topics` creates or updates a single-user topic preference:

```json
{
  "name": "local-first AI",
  "description": "Self-hosted agents, retrieval, and private inference",
  "weight": 1.5
}
```

### Repositories

See `docs/reference/api-contracts.md` for full request/response schemas.

- `GET /v1/repositories` -- list with filters (`is_starred`, `language`, `topic`, `source`, `pending_analysis`, `sort`, `limit`, `offset`)
- `GET /v1/repositories/watched` -- list repositories watched for README/release deltas
- `GET /v1/repositories/{id}` -- full detail including `analysis`
- `POST /v1/repositories` -- ingest by URL (202, non-blocking)
- `POST /v1/repositories/{id}/watch` -- create or update a watch; body defaults to `{"watch_readme": true, "watch_releases": true}`
- `DELETE /v1/repositories/{id}/watch` -- remove a watch
- `POST /v1/repositories/{id}/reanalyze` -- force LLM reanalysis
- `DELETE /v1/repositories/{id}` -- 204
- `GET /v1/search/repositories` -- semantic search with `q`, filters, `min_similarity`

Example list request:

```http
GET /v1/repositories?is_starred=true&language=rust&limit=20
Authorization: Bearer eyJ...
```

Example ingest request:

```http
POST /v1/repositories
Authorization: Bearer eyJ...
Content-Type: application/json

{
  "url": "https://github.com/rust-lang/rust"
}
```

Response:

```json
{
  "success": true,
  "data": {
    "repository_id": "a1b2c3d4-...",
    "status": "pending",
    "full_name": "rust-lang/rust"
  }
}
```

### GitHub Auth

- `POST /v1/auth/github/pat` -- connect via Personal Access Token
- `POST /v1/auth/github/device/start` -- begin Device Flow
- `POST /v1/auth/github/device/poll` -- poll Device Flow status
- `GET /v1/auth/github/status` -- connection status + repo_count
- `DELETE /v1/auth/github` -- revoke integration (204)

### Search and Topics

- `GET /v1/search`
- `GET /v1/search/semantic`
- `GET /v1/search/insights`
- `GET /v1/topics/trending`
- `GET /v1/topics/related`

### Collections and Collaboration

- `GET /v1/collections`
- `POST /v1/collections`
- `GET /v1/collections/tree`
- `GET /v1/collections/{collection_id}`
- `PATCH /v1/collections/{collection_id}`
- `DELETE /v1/collections/{collection_id}`
- `POST /v1/collections/{collection_id}/move`
- `POST /v1/collections/{collection_id}/reorder`
- `GET /v1/collections/{collection_id}/items`
- `POST /v1/collections/{collection_id}/items`
- `DELETE /v1/collections/{collection_id}/items/{summary_id}`
- `POST /v1/collections/{collection_id}/items/reorder`
- `POST /v1/collections/{collection_id}/items/move`
- `POST /v1/collections/{collection_id}/share`
- `DELETE /v1/collections/{collection_id}/share/{target_user_id}`
- `GET /v1/collections/{collection_id}/acl`
- `POST /v1/collections/{collection_id}/invite`
- `GET /v1/collections/invites/incoming`
- `POST /v1/collections/invites/{token}/accept`

### User Profile

- `GET /v1/users/me`
- `PUT /v1/users/me`
- `POST /v1/users/me/onboarding/complete`
- `POST /v1/users/me/feed-token`
- `DELETE /v1/users/me/feed-token`
- `GET /v1/users/me/feed.xml?token=...`
- `GET /v1/user/preferences`
- `PATCH /v1/user/preferences`
- `GET /v1/user/stats`

`GET /v1/users/me` returns `{ profile }` with typed fields: `userId`, `telegramUsername`, `displayName`, `locale`, `theme`, `defaultSummaryLanguage`, `onboardingCompletedAt`, `createdAt`, and `updatedAt`. `PUT /v1/users/me` accepts `locale`, `theme` (`dark`, `light`, `system`), `displayName`, and `defaultSummaryLanguage` (`auto`, `en`, `ru`) and returns the same shape. `POST /v1/users/me/onboarding/complete` sets `onboardingCompletedAt` and returns the updated profile. Legacy `/v1/user/preferences` remains supported and mirrors `lang_preference` / `app_settings.theme` into typed profile fields for backwards compatibility.

`POST /v1/users/me/feed-token` rotates the current user's public library feed token and returns `token` plus `feedUrl`. Only the newest active token works; rotation revokes the previous feed URL. `DELETE /v1/users/me/feed-token` revokes the active token. `GET /v1/users/me/feed.xml?token=<rss-token>` is intentionally bearer-auth-free for feed readers and resolves the user from the token. It returns Atom 1.0 XML with `Cache-Control: private, max-age=300` and a strong `ETag`; callers may send `If-None-Match` and receive `304 Not Modified` when the feed is unchanged. Optional filters are `tag=<topic>`, `collection=<collection-id-or-name>`, `language=en|ru`, and `limit=1..100`.

Obsidian usage example: rotate a token from an authenticated API session, then paste the returned `feedUrl` into a feed-reader plugin. To subscribe to one collection only, append `&collection=Reading%20Queue`; to subscribe to Russian summaries only, append `&language=ru`. Treat the URL as a secret because anyone with it can read the token-scoped feed until it is rotated or revoked.

### Notifications and Proxy

- `POST /v1/notifications/device`
- `GET /v1/proxy/image`

### Digest (Telegram Mini App)

- `GET /v1/digest/channels`
- `POST /v1/digest/channels/subscribe`
- `POST /v1/digest/channels/unsubscribe`
- `GET /v1/digest/preferences`
- `PATCH /v1/digest/preferences`
- `GET /v1/digest/history`
- `POST /v1/digest/trigger`
- `POST /v1/digest/trigger-channel`

### System Maintenance

- `GET /v1/system/db-dump`
- `HEAD /v1/system/db-dump`
- `GET /v1/system/db-info`
- `POST /v1/system/clear-cache`

## Notes

- Use OpenAPI for request and response schema details, enums, constraints, and examples.
- If this summary diverges from implementation, treat `docs/openapi/mobile_api.yaml` as authoritative.
