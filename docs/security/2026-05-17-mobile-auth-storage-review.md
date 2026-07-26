# Security review note: mobile auth, secret-login, client storage

> Historical draft captured on 2026-05-17. It is not a current security
> assessment: several listed gaps, including logout-all and refresh-token family
> metadata, were implemented later. Use
> [Mobile API](../reference/mobile-api.md#authentication) and current tests/code
> for the live contract; preserve this file only as review history.

| field | value |
| --- | --- |
| date | 2026-05-17 |
| owner | Security Engineer |
| status | **DRAFT — flow inventory populated; risk classification + release-blocker calls await Security Engineer sign-off** |
| blocked_by | [[decide-auth-security-second-wave-scope]] |
| references | [[review-mobile-auth-threat-model]] |

This note is a structured frame for the Security Engineer to record the threat model and release-readiness checklist for mobile auth and client storage. The mechanical flow inventory (file pointers, surface list) is pre-populated from the codebase as of commit HEAD. The **risk classification per flow**, the **release-blocker calls**, and the **must-have-test list** require Security Engineer judgement and are marked `_AWAITING SECURITY ENGINEER_`.

This review is gated by the CTO decisions in [[decide-auth-security-second-wave-scope]]; until those decisions are recorded, the release-readiness checklist below cannot be completed.

---

## Flow inventory

Historical inventory of auth/session/client-storage surfaces as observed on 2026-05-17. It is retained as review evidence, not as a current file map.

### Backend (this repo)

| Flow | Files |
| --- | --- |
| Telegram WebApp init-data login | `app/api/routers/auth/endpoints_telegram.py`, `app/api/routers/auth/webapp_auth.py` |
| Refresh-token rotation | `app/api/routers/auth/endpoints_sessions.py`, `app/api/routers/auth/tokens.py` |
| Logout (single device) | `app/api/routers/auth/endpoints_sessions.py` (`POST /v1/auth/logout`) |
| Logout-all (every device) | **not yet implemented** — see [[harden-refresh-token-rotation-revocation]] follow-up |
| Secret-key creation/rotation/revocation | `app/api/routers/auth/endpoints_secret_keys.py` |
| Secret-login attempt + decoy-PHC compare | `app/api/routers/auth/credential_auth.py`, `app/api/routers/auth/secret_auth.py` |
| Telegram nonce verification (must be constant-time) | `app/api/routers/auth/webapp_auth.py` |
| Account deletion | `app/api/routers/auth/endpoints_me.py` |
| Session listing | `app/api/routers/auth/endpoints_sessions.py` |
| Bearer dependency / middleware | `app/api/routers/auth/dependencies.py`, `app/api/middleware.py` |
| Allowlist enforcement | `app/api/routers/auth/dependencies.py` + `ALLOWED_USER_IDS` env |
| AuditLog write paths | `app/db/models/core.py:AuditLog` + grep `audit_log.add` |
| Refresh-token storage | `app/db/models/core.py:RefreshToken` (post [[harden-refresh-token-rotation-revocation]]: add `family_id`, `parent_token_hash`) |

### Client (ratatoskr-client repo — not in this checkout)

Per the task spec, the KMP client stores tokens in platform secure storage (Tink AEAD / DataStore on Android, KeychainSettings on iOS) and uses Ktor bearer refresh. **File-level inventory for the client must be filled in from the ratatoskr-client repo.**

| Flow | File (in ratatoskr-client) |
| --- | --- |
| Token persistence (Android) | `_AWAITING SECURITY ENGINEER`  |
| Token persistence (iOS) | `_AWAITING SECURITY ENGINEER`  |
| Ktor `AuthProvider` bearer + refresh wiring | `_AWAITING SECURITY ENGINEER`  |
| App-resume secure-storage read | `_AWAITING SECURITY ENGINEER`  |
| `clearSavedCredentials` UX surface | `_AWAITING SECURITY ENGINEER`  |

---

## Per-flow risk classification

For each flow above, the Security Engineer records:

- **Risk rating**: P0 / P1 / P2 / informational
- **Must-have evidence** (test name, log assertion, manual probe)
- **Release-blocker**: yes / no
- **Owner of remediation** (if blocker)

| Flow | Risk | Evidence required | Release-blocker | Remediation owner |
| --- | --- | --- | --- | --- |
| Refresh-token rotation | _AWAITING_ | _AWAITING_ | _AWAITING_ | _AWAITING_ |
| Logout (single device) | _AWAITING_ | _AWAITING_ | _AWAITING_ | _AWAITING_ |
| Logout-all (every device) | _AWAITING_ | _AWAITING_ | likely **yes** (currently unimplemented) | Backend |
| Secret-login one-time plaintext | _AWAITING_ | _AWAITING_ | _AWAITING_ | _AWAITING_ |
| Telegram nonce constant-time | _AWAITING_ | constant-time compare test in `tests/security/` | _AWAITING_ | Backend |
| Account deletion completeness | _AWAITING_ | tombstone test covering RefreshToken, AuditLog, Summary backrefs | _AWAITING_ | Backend |
| Secure storage (Android) | _AWAITING_ | _AWAITING_ | _AWAITING_ | KMP |
| Secure storage (iOS) | _AWAITING_ | _AWAITING_ | _AWAITING_ | KMP |

---

## Release-blocking ambiguities

The task spec asks for explicit flags on:

### Fail-open allowlist behaviour

`ALLOWED_USER_IDS` is read into a static `set[int]` at startup. Open question: does the dependency _fail open_ when the env var is empty or malformed (allowing any user) or _fail closed_ (denying all)?

**_AWAITING SECURITY ENGINEER call_** — verification probe:

```sh
ALLOWED_USER_IDS="" pytest tests/api/test_auth_allowlist.py
```

### External client IDs

Open question: are external client_id values rate-limited or distinguishable from owner clients? `app/db/models/core.py:ClientSecret` holds the secrets table; review the issuance flow for trust-elevation mistakes.

**_AWAITING SECURITY ENGINEER call_**

### Hosted MCP / CLI access

Per [[decide-auth-security-second-wave-scope]] §4, no external exposure expansion is permitted without explicit CTO approval. This flag remains **closed** unless that decision is recorded.

---

## Items requiring explicit approval

Per task constraints, the following changes need an approval line in this note before any code lands:

- [ ] External access expansion (MCP, CLI, web UI scope creep)
- [ ] Credential policy changes (rotation cadence, complexity, show-once strategy)
- [ ] Telemetry / privacy scope changes (new fields persisted, new retention windows, new analytics destinations)
- [ ] Production release sign-off

Each item ships with: who approved, on what date, with what mitigations.

---

## Definition of done

This note is "done" when:

1. CTO has recorded decisions in [[decide-auth-security-second-wave-scope]].
2. The per-flow risk classification table above is fully populated by the Security Engineer.
3. The release-blocking ambiguities section has explicit verdicts.
4. Every flag in "Items requiring explicit approval" has either an approval line or an explicit "rejected with rationale".
5. The release-readiness checklist is publishable to QA as a completable list — not a draft.
