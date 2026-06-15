# ADR 0009: Hosted MCP external exposure (approved in principle)

**Date:** 2026-06-15
**Status:** Accepted in principle — a dedicated design + security review is required before any implementation. This ADR records the approval and its guardrails; it is **not** an implementation green-light.

## Context

The [auth second-wave memo](2026-05-17-auth-security-second-wave.md) Decision 4 (2026-06-15) approved hosting the MCP server (`app/mcp/server.py`) externally, beyond the owner whitelist (`ALLOWED_USER_IDS`). The CLI runner (`app/cli/`) stays local-only. This changes the MCP threat model from owner-only to externally reachable, which is a significant security expansion.

## Decision (in principle)

Expose MCP externally under a **per-call auth posture**. The following are **mandatory** before any rollout:

- **Authentication on every MCP call** (not merely the `ALLOWED_USER_IDS` gate) with per-caller identity.
- **Rate-limiting + per-call cost attribution.** MCP calls drive LLM and scrape spend (real money — cf. the Webwright cost note in CLAUDE.md rule 9); each caller's spend must be bounded and attributed.
- **Abuse controls:** input validation at the trust boundary, per-caller quotas.
- **A dedicated threat-model + security review gate** ([[review-mobile-auth-threat-model]] re-opened) before build.

## Consequences

- Introduces multi-caller concerns (rate-limiting, quotas, cost attribution, abuse) into a previously owner-only surface. A separate **implementation ADR** is required once the design exists.
- The CLI runner remains local-only and is unaffected.
- Until the design + security review lands, **MCP stays owner-only** — no partial exposure.
