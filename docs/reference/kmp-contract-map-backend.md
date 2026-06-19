# Ratatoskr ↔ ratatoskr-client mobile contract map

The cross-repo contract map called for by [[map-ratatoskr-mobile-api-contract-to-kmp-readiness]]. Inventories the 146 `/v1/*` endpoints currently in `docs/openapi/mobile_api.yaml`, grouped by feature area, with source-of-truth pointers on **both** sides of the wire.

| field | value |
| --- | --- |
| backend openapi spec | `docs/openapi/mobile_api.yaml` (146 paths) |
| envelope | `{ "ok": true, "data": …, "pagination": … }` — see `app/api/models/responses/common.py` |
| error envelope | `{ "ok": false, "error": { code, message, correlation_id } }` |
| auth | bearer JWT or Telegram WebApp `X-Telegram-Init-Data` header |
| client repo | `ratatoskr-client/` — KMP (Android, iOS, Desktop) |
| client HTTP layer | `core/data/src/commonMain/.../data/remote/ApiClient.kt` — Ktor `Auth` plugin with `BearerTokens` + refresh chain |
| client secure storage | `core/data/src/commonMain/.../data/local/SecureStorage.kt` with platform expects in `androidMain/AndroidSecureStorage.kt`, `iosMain/IosSecureStorage.kt`, `desktopMain/DesktopSecureStorage.kt` |
| client feature modules | `feature/{auth,collections,digest,settings,summary,sync}/src/commonMain/.../{presentation,data,domain}/` |

## Feature areas

### Authentication

| Surface | Path | Backend file | Client file (ratatoskr-client) |
| --- | --- | --- | --- |
| Telegram login | `POST /v1/auth/telegram-login` | `app/api/routers/auth/endpoints_telegram.py` | `feature/auth/.../data/repository/AuthRepositoryImpl.kt`; `presentation/viewmodel/AuthViewModel.kt` |
| Telegram WebApp linking | `POST /v1/auth/me/telegram/link`, `/complete` | same | `feature/auth/.../data/mappers/TelegramLinkStatusMapper.kt` |
| Refresh | `POST /v1/auth/refresh` | `app/api/routers/auth/endpoints_sessions.py` | `core/data/.../data/remote/ApiClient.kt:206-239` — Ktor `BearerTokens` refresh chain; transparent to callers |
| Logout (current device) | `POST /v1/auth/logout` | same | `feature/auth/.../presentation/viewmodel/AuthViewModel.kt:141` — `logout(clearSavedCredentials: Boolean)` |
| Logout-all | **backend not implemented** | tracked under [[harden-refresh-token-rotation-revocation]] follow-up | client surface not yet exposed |
| Session list | `GET /v1/auth/sessions` | same | Settings module (likely; verify location) |
| Single session revoke | `DELETE /v1/auth/sessions/{session_id}` | same | same |
| Secret-login | `POST /v1/auth/secret-login` | `app/api/routers/auth/endpoints_credentials.py` (constant-time compare in `credential_auth.py`) | `feature/auth/.../data/repository/AuthRepositoryImpl.kt:81` — `AuthenticationApi.secretLoginV1AuthSecretLoginPost(...)` |
| Secret-key CRUD | `GET/POST /v1/auth/secret-keys`, `…/rotate`, `…/revoke` | `endpoints_secret_keys.py` | not yet wired client-side per code search |
| Credentials change-password | `POST /v1/auth/credentials/change-password` | `endpoints_credentials.py` | `feature/auth/.../data/repository/CredentialsRepositoryImpl.kt` |
| Me / Telegram linkage | `GET /v1/auth/me`, `GET /v1/auth/me/telegram` | `endpoints_me.py`, `endpoints_telegram.py` | `feature/auth/.../data/repository/UserRepositoryImpl.kt` |
| GitHub OAuth / PAT | `/v1/auth/github`, `.../device/start\|poll`, `.../pat` | `app/api/routers/auth/github.py` (Fernet-encrypted at rest) | not yet wired client-side per code search |

### Summaries / articles

| Surface | Path | Backend file | Client file (ratatoskr-client) |
| --- | --- | --- | --- |
| List | `GET /v1/summaries` | `app/api/routers/content/summaries.py:162` (now supports `search=`, plus the existing `is_read`, `is_favorited`, `lang`, `start_date`, `end_date`, `sort`) | `feature/summary/` — repository + viewmodel; verify the client passes the new `search` query param |
| Articles alias | `GET /v1/articles…` | same router; alias surface | client may use either naming |
| Detail | `GET /v1/summaries/{id}` | `summaries.py:289` | `feature/summary/` |
| Content body | `GET /v1/summaries/{id}/content` | `summaries.py:412` | same |
| Export | `GET /v1/summaries/{id}/export` | `summaries.py:480` (PDF via weasyprint) | same |
| Toggle favorite | `POST /v1/summaries/{id}/favorite` | `summaries.py:611` | same |
| Bulk mark-read | `POST /v1/summaries/bulk/mark-read` | `summaries.py` (shipped 2026-05-17) | **not yet wired client-side** |
| Bulk favorite | `POST /v1/summaries/bulk/favorite` | `summaries.py` (shipped 2026-05-17) | **not yet wired client-side** |
| Bulk delete | `POST /v1/summaries/bulk/delete` | `summaries.py` (shipped 2026-05-17) | **not yet wired client-side** |
| Reading progress | `PATCH /v1/summaries/{id}/reading-position` | `summaries.py:559` | same |
| Feedback | `POST /v1/summaries/{id}/feedback` | `summaries.py:624` | same |
| Soft delete | `DELETE /v1/summaries/{id}` | `summaries.py:585` | same |
| Recommendations | `GET /v1/summaries/recommendations` | `summaries.py:235` | same |
| Highlights | `GET/POST/DELETE /v1/summaries/{id}/highlights/…` | `app/api/routers/content/` | `feature/summary/.../data/repository/HighlightRepositoryImpl.kt` |
| Search | `GET /v1/search` | `app/api/routers/content/search.py` | `feature/summary/.../data/repository/SearchRepositoryImpl.kt` |
| Audio (TTS) | `GET /v1/summaries/{id}/audio`; `POST /v1/summaries/audio/playlist`; `GET/PUT /v1/users/me/tts-preferences` | `app/api/routers/user/tts.py` (ElevenLabs) | `feature/summary/.../data/repository/AudioRepositoryImpl.kt` |

### Sync

| Surface | Path | Backend file | Client file (ratatoskr-client) |
| --- | --- | --- | --- |
| Session list | `GET /v1/sync/sessions` | `app/api/routers/sync.py` | `feature/sync/.../data/repository/SyncRepositoryImpl.kt`; `feature/sync/.../feature/sync/domain/repository/SyncRepository.kt` |
| Full sync | `GET /v1/sync/full` | same | same |
| Delta sync | `GET /v1/sync/delta` | same (server_version watermark) | same |
| Apply mutation | `POST /v1/sync/apply` | same (conflict resolution server-authoritative) | same |
| Device register | `POST /v1/notifications/device` | `app/api/routers/notifications.py` | `feature/settings/.../data/repository/NotificationRepositoryImpl.kt` |

### Collections

| Surface | Path | Backend file | Client file (ratatoskr-client) |
| --- | --- | --- | --- |
| List + CRUD | `GET/POST /v1/collections`, `…/{id}` | `app/api/routers/content/` | `feature/collections/` — see `RssRepositoryImpl.kt`, `ImportExportRepositoryImpl.kt`, `RuleRepositoryImpl.kt` |
| Items | `POST/DELETE /v1/collections/{id}/items/{summary_id}` | same | `feature/collections/` |
| Reorder | `POST /v1/collections/{id}/reorder`, `/items/reorder` | same | same |
| ACL | `GET/POST /v1/collections/{id}/acl`, `/share/…` | same | same |
| Invite | `POST /v1/collections/{id}/invite`, `/invites/{token}/accept` | same | same |
| Tree | `GET /v1/collections/tree` | same | same |

### Digest

| Surface | Path | Backend file | Client file (ratatoskr-client) |
| --- | --- | --- | --- |
| Custom digest CRUD | `GET/POST/DELETE /v1/digests/custom…` | `app/api/routers/digest.py` | `feature/digest/.../data/repository/CustomDigestRepositoryImpl.kt`; `feature/digest/.../domain/repository/DigestRepository.kt` |
| Channel digest reading | (via subscriptions endpoints) | `app/api/routers/digest.py` | `feature/digest/.../data/repository/DigestRepositoryImpl.kt` |
| Quick-save | `POST /v1/quick-save` | `app/api/routers/quick_save.py` | (verify in collections or summary module) |

### Signals / aggregation

| Surface | Path | Backend file | Client file (ratatoskr-client) |
| --- | --- | --- | --- |
| Signals stream | `GET /v1/signals` | `app/api/routers/signals.py` | not yet wired client-side per code search |
| Source toggle | `POST /v1/signals/sources/{id}/active` | same | same |
| Signal feedback | `POST /v1/signals/{id}/feedback` | same | same |
| Sources / topics health | `GET /v1/signals/health`, `…/sources/health` | same | same |
| Topics index | `GET /v1/signals/topics` | same | same |

### Repositories (GitHub ingestion)

| Surface | Path | Backend file | Client file (ratatoskr-client) |
| --- | --- | --- | --- |
| List + CRUD | `GET/POST /v1/repositories`, `…/{id}` | `app/api/routers/repositories.py` | not yet wired client-side per code search |
| Re-analyze | `POST /v1/repositories/{id}/reanalyze` | same | same |
| Search | `GET /v1/search/repositories` | same | same |

### Goals / streaks

| Surface | Path | Backend file | Client file (ratatoskr-client) |
| --- | --- | --- | --- |
| Goal CRUD | `GET/POST/DELETE /v1/user/goals…` | `app/api/routers/user.py` | `feature/settings/.../data/repository/ReadingGoalRepositoryImpl.kt` |
| Streak | `GET /v1/user/streak` | same | same |
| Goal progress | `GET /v1/user/goals/progress` | same | same |
| Backups | `GET/POST /v1/backups…` | `app/api/routers/backups.py` | `feature/settings/.../data/repository/BackupRepositoryImpl.kt` |

### Account / admin

| Surface | Path | Backend file | Client file (ratatoskr-client) |
| --- | --- | --- | --- |
| Account self | `GET /v1/auth/me` | `app/api/routers/auth/endpoints_me.py` | `feature/auth/.../data/repository/UserRepositoryImpl.kt` |
| Account delete | `DELETE /v1/auth/me` (verify in spec) | same | `feature/auth/` — verify deletion completeness in security review |
| Admin surfaces | `/v1/admin/users`, `/v1/admin/jobs`, `/v1/admin/metrics`, `/v1/admin/audit-log`, `/v1/admin/health/content` | `app/api/routers/admin/` (owner-only via `ALLOWED_USER_IDS`) | not exposed to mobile client |

### Misc

| Surface | Path | Backend file | Client file (ratatoskr-client) |
| --- | --- | --- | --- |
| Health | `/v1/healthz`, `/v1/readyz`, `/v1/admin/health/content` | `app/api/routers/health.py` | not consumed by mobile client |
| Image proxy | `GET /v1/proxy/image` | `app/api/routers/proxy.py` | consumed via image-loading layer |
| Notifications device | `POST /v1/notifications/device` | `app/api/routers/notifications.py` | `feature/settings/.../data/repository/NotificationRepositoryImpl.kt` |

## Gaps / drift identified by this cross-repo walk

| # | Surface | Backend state | Client state | Recommended follow-up |
| --- | --- | --- | --- | --- |
| 1 | Logout-all | Endpoint not implemented; policy module ready (`app/security/token_family_policy.py`) | UI surface not exposed | File backend follow-up to wire `POST /v1/auth/logout-all` per [[harden-refresh-token-rotation-revocation]] follow-up; KMP can add the menu item after |
| 2 | Refresh-token family revocation | Decision policy + DB columns ready (migration 0016); endpoint not consulting policy yet | Ktor refresh chain transparently retries — does not know about family revocation | Same backend follow-up; client behaviour unchanged because the Ktor chain only sees 401 / 200 from `POST /v1/auth/refresh` |
| 3 | New bulk endpoints (mark-read / favorite / delete) | Shipped 2026-05-17 | Not wired | KMP follow-up: regenerate `core/api-generated` from the updated `mobile_api.yaml` then expose bulk-action use cases in `feature/summary` |
| 4 | Server-side `search` on `GET /v1/summaries` | Shipped 2026-05-17 | Client repo likely passes only legacy params | KMP follow-up: pipe a search-term parameter through the summary list repository |
| 5 | Signal-score field on `SummaryCompact` | `confidence` already exists; no separate `signal_score` field added | KMP / web both filter on `confidence` | If a distinct semantic is wanted, file a backend issue with the formula (per [[overhaul-articles-management]] audit) |
| 6 | Account deletion completeness | Endpoint exists; cascade depth unverified | Client calls the endpoint and clears local state | Security review (`docs/security/2026-05-17-mobile-auth-storage-review.md`) §2 must verify Refresh family + AuditLog redaction + Summary backrefs + Qdrant points all get scrubbed |
| 7 | Secret-keys CRUD on client | Endpoint shipped | No client wiring found | Product/UX decision: do we expose secret-key rotation in the mobile UI? Tracked by [[decide-auth-security-second-wave-scope]] §2 |
| 8 | TLS pinning, secret show-once strategy, default `clearSavedCredentials` UX | Awaiting CTO decisions in `docs/decisions/2026-05-17-auth-security-second-wave.md` | `AuthViewModel.kt:141` exposes `clearSavedCredentials` as a parameter but the default surface is product/UX | Resolve via the CTO memo, then the KMP UI follows the chosen default |

## Security / privacy notes per task spec

- Bearer tokens are issued by `app/api/routers/auth/tokens.py`; HS256-signed.
- Refresh-token storage: backend hashes (`RefreshToken.token_hash`), cookie-mode for web origin only, bearer-mode response field for native clients.
- Secret-keys at rest: hashed (PHC); decoy PHC kept in a module holder so timing-safe-compare is independent of registered users.
- GitHub OAuth / PAT tokens: Fernet-encrypted at rest via `app/security/token_crypto.py`.
- Telegram WebApp init-data: validated server-side every request; no caching of the verified payload across requests.
- Client-side: tokens live in platform secure storage (`SecureStorage` expect-actual triad); Ktor `BearerTokens` chain in `ApiClient.kt:206-239` handles transparent refresh.

## Definition of done

This file plus the two security/decision frames (`docs/decisions/2026-05-17-auth-security-second-wave.md`, `docs/security/2026-05-17-mobile-auth-storage-review.md`) constitute the cross-repo contract baseline for the next mobile release gate. The 8 follow-ups above are the action list; ownership (Backend, KMP, Security, CTO) is called out per row.
