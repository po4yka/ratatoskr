# ADR 0019: Health endpoint envelope carve-out

**Date:** 2026-06-18
**Status:** Accepted

## Context

Ratatoskr's Mobile API contract standardizes JSON business responses around `success`, `data`, `meta`, and standardized `error`. Health endpoints are different: `/health`, `/health/live`, `/health/ready`, and `/health/detailed` are consumed by Docker, Kubernetes-style probes, uptime monitors, and operators before authenticated user traffic is safe to serve. These callers often only inspect status codes or a small top-level probe field and may not carry bearer credentials.

## Decision

Health endpoints are a documented probe carve-out from the business-response envelope rule. Successful health responses may use the standard success envelope when convenient, but clients must not rely on the business envelope for probe failure paths. In particular, readiness failure can return a raw probe object with top-level `ready`, `error`, and `timestamp` so infrastructure monitors can parse it without understanding the mobile envelope.

Generated OpenAPI descriptions and `docs/reference/mobile-api.md` must call this out explicitly. Mobile clients that need user-facing diagnostics should prefer authenticated business endpoints such as `/v1/meta`, `/v1/admin/diagnostics` for owner workflows, or domain-specific health endpoints under `/v1/*` that already use the normal envelope.

## Consequences

- Liveness/readiness integrations keep simple status-code/top-level-field parsing.
- The mobile business API still has a clear envelope invariant, with health listed as an explicit exception rather than silent drift.
- Any future user-facing diagnostics endpoint should be added under `/v1` and use the standard envelope instead of expanding this carve-out.
