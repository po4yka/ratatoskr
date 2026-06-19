# ADR 0020: Related reads HTTP surface

**Date:** 2026-06-19
**Status:** Accepted

## Context

`app/application/services/related_reads_service.py` already backs Telegram follow-up messages, but the HTTP API exposed only tag-based recommendations and search-topic related summaries. Leaving the vector related-reads service without an HTTP consumer makes web and mobile clients reimplement a different recommendation path and lets the Telegram and API product surfaces drift.

## Decision

Expose `GET /v1/summaries/{summary_id}/related` through the existing summaries router, with `/v1/articles/{summary_id}/related` inherited from the article alias mount. The endpoint first loads the summary through `SummaryReadModelUseCase.get_summary_context_for_user`, so non-owned or deleted summaries remain indistinguishable from missing summaries. It then invokes `RelatedReadsService` with a request-scoped vector-search adapter bound to the authenticated `user_id` and configured vector `user_scope`.

## Consequences

- Telegram and HTTP now share the same related-reads application service and similarity threshold configuration.
- The API response is intentionally small: source `summaryId`, related item IDs, titles, age labels, similarity scores, and count.
- The endpoint depends on vector search availability and respects `RELATED_READS_ENABLED`; disabled deployments return the standard `FEATURE_DISABLED` API error.

## Alternatives rejected

- **Delete the service** -- rejected because the service is active in Telegram follow-up flows.
- **Reuse `/v1/summaries/recommendations`** -- rejected because that endpoint is tag/history-based, not source-summary vector similarity.
