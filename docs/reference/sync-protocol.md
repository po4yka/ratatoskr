# Sync Protocol

Implementation map for the offline sync surface used by mobile clients. This is a concise owner map, not a broad product spec.

## Contract

| Step | Endpoint | Backend owner | Notes |
|---|---|---|---|
| Create/resume session | `POST /v1/sync/sessions` | `app/api/routers/sync.py`, `app/api/services/sync/service.py`, `app/api/services/sync/session_store.py` | Returns a session payload only; pagination metadata belongs to full and delta page responses. Session storage uses Redis when available and falls back to in-process memory. |
| Full sync | `GET /v1/sync/full?session_id=...&limit=...` | `app/api/services/sync/collector.py`, `app/infrastructure/persistence/sync_aux_read_adapter.py` | Bounded initial chunks. Keep cursor/limit behavior aligned with generated OpenAPI. |
| Delta sync | `GET /v1/sync/delta?session_id=...&cursor=...&limit=...` | `app/api/routers/sync.py::_build_delta_etag`, `app/api/services/sync/collector.py` | Emits created/updated/deleted records since the cursor and ETag keyed by session plus max server version. |
| Apply changes | `POST /v1/sync/apply` | `app/api/services/sync/apply.py`, `app/api/services/sync/service.py` | Applies client-side changes with per-item results and idempotency handling where the request model provides it. |

## Generated Artifacts

The backend OpenAPI source is `app.api.main:app`; committed generated artifacts are `docs/openapi/mobile_api.yaml` and `docs/openapi/mobile_api.json`. Do not edit them manually. After changing sync routers, request models, response models, or envelope versioning, run:

```bash
make generate-openapi
make check-openapi-drift
make check-openapi-validate
make check-openapi
```

Downstream clients regenerate from the committed backend spec using the workflow documented in their own repositories. See [OpenAPI Contract Workflow](openapi-contract-workflow.md).

## Client Ownership

| Client | Ownership | Generated boundary |
|---|---|---|
| KMP | Offline sync orchestration is owned by the external KMP repository. | Do not hand-edit its generated OpenAPI client. |
| Web | No backend-owned offline-sync implementation; request-progress streaming is owned by the external web repository. | Do not hand-edit its generated OpenAPI client. |

## Failure Links

| Symptom | First files | Triage doc |
|---|---|---|
| Sync drift or stale generated client | `docs/openapi/mobile_api.yaml`, `tests/api/test_runtime_openapi_drift.py`, `tests/tools/test_generate_openapi.py` | `docs/reference/openapi-contract-workflow.md` |
| Sync conflict | `app/api/services/sync/apply.py`, `app/api/services/sync/collector.py` | `docs/reference/troubleshooting.md#sync-conflicts` |
| Auth/session failure during sync | `app/api/routers/auth/`, `app/api/routers/auth/endpoints_sessions.py` | `docs/reference/troubleshooting.md#refresh-token-stops-working` |
| Request processing stuck after submit | `app/adapters/content/graph_url_processor.py`, `app/application/graphs/summarize/`, `app/db/models/core.py::RequestProcessingJob`, `app/adapters/content/streaming/` | `docs/reference/troubleshooting.md#request-stuck-in-processing` |
