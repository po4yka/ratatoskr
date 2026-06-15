# Decision memo: Ratatoskr auth/security second-wave scope

| field | value |
| --- | --- |
| date | 2026-05-17 (decisions recorded 2026-06-15) |
| owner | CTO |
| status | **DECIDED — CTO sign-off recorded 2026-06-15** |
| references | [[review-mobile-auth-threat-model]], [[decide-auth-security-second-wave-scope]] |

This memo framed the five second-wave policy questions surfaced by the Security / AppSec review. As of **2026-06-15** the owner has recorded a decision on each. Each numbered section ends with a resolved `DECISION:` line plus the task-spec classification. Implementation of the resolved items is tracked on the task board (see *Follow-up issue inventory*).

---

## 1. TLS pinning policy

Question: should the KMP and web clients pin the production TLS certificate (chain or leaf), and if so under which rotation policy?

Evidence to weigh:

- Pinning reduces MITM risk on user-controlled devices but introduces a brick-the-app failure mode if a cert rotates outside the pinned window. Mobile clients especially can become unusable if the pin outlives the cert.
- The current `ratatoskr-client` storage uses platform secure storage; pinning would add a parallel trust anchor.
- Operational cost: a documented rotation runbook, ideally with multi-pin transition windows.

Implementation surface (if approved):

- `ratatoskr-client` Ktor `HttpClient` engine config — Android `OkHttpEngine.certificatePinner`, iOS `URLSession` pinning via `URLSessionDelegate.urlSession(_:didReceive:completionHandler:)`.
- Web: HSTS already covers transport; pin via `Expect-CT` if revived.

**DECISION (2026-06-15):** **Pin the production certificate (leaf + intermediate) on KMP and web clients**, under a **multi-pin rotation policy**: keep ≥2 active pins with a transition window so a cert rotation never outlives its pin. A documented rotation runbook is a prerequisite for rollout. Classification: **[x] implementation follow-up**.

---

## 2. Secret show-once strategy

Question: when a user creates a long-lived secret key via the `secret-login` flow, do we show it plaintext exactly once, or do we always send it to the user's verified channel (Telegram DM) and never display it in the UI?

Evidence to weigh:

- Show-once is the industry default but trains users to screenshot secrets, weakening the storage assumptions.
- DM-only delivery requires the user has Telegram open and means the UI carries no secret — but it also fragments the UX across two surfaces.
- Either way, the **decoy PHC** path (`app/api/routers/auth/credential_auth.py:_DECOY_PHC`) must keep the timing-safe-compare guarantee.

Implementation surface (if approved):

- Show-once: extend the secret-key creation endpoint with a one-time `display_token` field whose lifetime is the UI render.
- DM-only: route plaintext through the existing Telegram bot notify path; UI returns metadata only.

**DECISION (2026-06-15):** **DM-only delivery.** The plaintext secret is sent once to the user's verified Telegram channel; the UI returns metadata only and never displays the secret. The decoy-PHC timing-safe-compare guarantee is retained. Classification: **[x] implementation follow-up**.

---

## 3. AuditLog retention

Question: what is the retention policy for `AuditLog` rows (refresh-family revocations, secret-login attempts, account deletions)?

Evidence to weigh:

- Current schema (`app/db/models/core.py:AuditLog`) is append-only with no retention enforcement.
- Privacy claim in user-facing docs may need explicit retention window to be coherent.
- Investigative value of audit history is high during incident response — but indefinite retention enlarges the blast radius of a database compromise.

Implementation surface (if approved):

- New Taskiq nightly job pruning rows older than the retention window with a `last_pruned_at` watermark.
- Migration: optional `pii_redacted: bool` column to flag rows that have been scrubbed but kept for aggregate stats.

**DECISION (2026-06-15):** **Retention window = 90 days.** A nightly Taskiq job prunes `AuditLog` rows older than 90 days with a `last_pruned_at` watermark; add an optional `pii_redacted` column to flag scrubbed-but-kept aggregate rows. Retention window: **90 days**. Classification: **[x] implementation follow-up**.

---

## 4. Hosted MCP / CLI external exposure scope

Question: do we expand external exposure of the MCP server (`app/mcp/server.py`) and the CLI runner (`app/cli/`) beyond the owner-whitelist (`ALLOWED_USER_IDS`)? If yes, under which auth posture?

Evidence to weigh:

- The MCP server currently inherits owner-only ACL; broadening it changes the threat model significantly (multi-tenant rate-limiting, per-call cost attribution, abuse vectors).
- The CLI runner is local-only and stays that way unless the decision explicitly approves a hosted variant.
- The task spec is explicit: "Explicit approval is requested before any external exposure expansion."

**DECISION (2026-06-15):** **Approved in principle to host the MCP server externally** beyond the owner whitelist, under a per-call auth posture (authentication, rate-limiting, per-call cost attribution, abuse controls). **The CLI runner stays local-only.** This is a large, separately-scoped track: a dedicated design + security review is required before any implementation begins. Classification: **[x] approval needed (granted; scoped design + security review to follow before build)**.

---

## 5. Default `clearSavedCredentials` UX

Question: in the mobile client, when the user signs out, do we clear saved credentials by default, or do we keep them so the user can re-enter without re-entering the secret?

Evidence to weigh:

- Default-clear matches a higher-security posture but increases friction; users will likely opt back into "remember me".
- Default-keep matches mainstream consumer UX but means a stolen device retains a usable session boundary, mitigated only by the OS-level secure storage and the refresh-token family rotation in [[harden-refresh-token-rotation-revocation]].

**DECISION (2026-06-15):** **Default-clear** saved credentials on sign-out; "remember me" is an explicit opt-in. This aligns with the pinning + DM-only posture decided above. Classification: **[x] product/UX**.

---

## Follow-up issue inventory

The four `implementation follow-up` decisions above should each get a task-board issue (`docs/tasks/issues/<slug>.md`):

- TLS certificate pinning (KMP + web) with multi-pin rotation runbook — *Decision 1*
- secret-login DM-only plaintext delivery (UI returns metadata only) — *Decision 2*
- AuditLog 90-day nightly Taskiq prune + `pii_redacted` column migration — *Decision 3*
- mobile sign-out default-clear credentials (`remember me` opt-in) — *Decision 5*

Decision 4 (hosted MCP) is `approval needed (granted)` and requires a dedicated **design + security-review** task before any implementation, given the threat-model change.

Backend's three high-priority blockers from the original review remain owned by the backend team and are tracked elsewhere:

- [[unify-allowed-user-ids-allowlist-semantics]]
- [[decouple-secret-login-pepper-from-jwt-key]]
- [[use-constant-time-compare-telegram-nonce]]

## Risks of not deciding

These constraints were in force while decisions were open and are now satisfied by the recorded decisions above:

- No release-readiness claim could be made until Security and QA sign off on the second-wave remediation that flows from these decisions. With decisions recorded, the remediation work can be scheduled and reviewed.
- The frontend-mobile task family ([[overhaul-articles-management]], [[run-frost-phase-7-mobile-regression]], [[map-ratatoskr-mobile-api-contract-to-kmp-readiness]]) was blocked while these auth decisions were open; it is now unblocked pending implementation of Decisions 1–5.
