# ADR 0008: Posture — expansion over single-tenant simplification

**Date:** 2026-06-15
**Status:** Accepted. Supersedes the retired ADR-0003 (deleted 2026-06-15).

## Context

A 2026-06 draft (ADR-0003) catalogued the multi-tenant scaffolding in this single-owner bot — per-user `WHERE` filters, refresh-token-family rotation, collections ACL/invites, sync-v2 sessions, the device registry — and proposed removing part of it. The one slice that was acted on (dropping `user_id` filters, `ae5c8b08`) was reverted the same day as an IDOR risk (`26375553`). Meanwhile the project chose to **expand** (LangGraph orchestration, RAG retrieval, hosted MCP). ADR-0003 was deleted on 2026-06-15 to avoid leaving a removal mandate that contradicts the direction.

## Decision

- Ratatoskr's single-tenant nature is a **deployment fact, not an architectural simplification target.** We do not pursue removal of the multi-tenant scaffolding.
- `user_id` / `user_scope` filters stay everywhere as defense-in-depth IDOR guards (CLAUDE.md rule 12). They are not "structural dead weight."
- JWT auth, refresh-token rotation + replay detection, collections (incl. the ACL surface), sync v2, the device registry, and `client_id` scoping are all **retained**.
- The one useful artifact from the 0003 work — the API-surface counter (`tools/count_api_endpoints.py`, surfaced in CI) — is kept as a *complexity-visibility* tool, not a removal trigger.

## Consequences

- Future "simplify single-tenant" audits should be **declined with reference to this ADR** — the round-trip (remove → revert) has already been paid for once.
- Complexity is managed by **visibility** (the endpoint counter, the file/class-size CI gates, radon) rather than by stripping isolation. Genuine over-engineering is still fair game; multi-tenant *isolation* specifically is not the target.
