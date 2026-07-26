# API errors

The REST API uses stable string error codes. Numeric families such as `AUTH001` or `DB001` are not part of the current contract.

The executable definitions are:

- `app/api/exceptions.py` for exception-to-response mappings;
- `app/api/models/responses/common.py` for response models and the shared error-code enum;
- `docs/openapi/mobile_api.yaml` for the generated public contract.

## Error envelope

A typical failure has this shape:

```json
{
  "success": false,
  "error": {
    "code": "VALIDATION_ERROR",
    "errorType": "validation_error",
    "message": "Request validation failed",
    "retryable": false,
    "details": {},
    "correlation_id": "01J...",
    "retry_after": null
  },
  "meta": {
    "correlation_id": "01J...",
    "timestamp": "2026-07-15T12:00:00Z",
    "version": "...",
    "api_version": "v1",
    "build": "..."
  }
}
```

Not every endpoint returns every optional field. Clients should branch on `error.code`, use the HTTP status for the broad category, and surface the correlation ID when reporting a failure.

## Code families

| Family | Current examples |
|---|---|
| Authentication | `UNAUTHORIZED`, `SESSION_EXPIRED`, `TOKEN_EXPIRED`, `TOKEN_INVALID`, `TOKEN_REVOKED`, `TOKEN_WRONG_TYPE`, `AUTH_TOKEN_EXPIRED`, `AUTH_CREDENTIALS_INVALID` |
| Authorization | `FORBIDDEN`, `AUTHZ_USER_NOT_ALLOWED`, `AUTHZ_CLIENT_NOT_ALLOWED`, `AUTHZ_OWNER_REQUIRED`, `AUTHZ_ACCESS_DENIED` |
| Validation and resources | `VALIDATION_ERROR`, `VALIDATION_FAILED`, `VALIDATION_FIELD_REQUIRED`, `VALIDATION_FIELD_INVALID`, `VALIDATION_URL_INVALID`, `NOT_FOUND`, `RESOURCE_NOT_FOUND`, `CONFLICT`, `RESOURCE_ALREADY_EXISTS`, `RESOURCE_VERSION_CONFLICT` |
| Rate limits | `RATE_LIMIT_EXCEEDED`, `REFRESH_RATE_LIMITED`, `github_oauth_rate_limited` |
| OAuth and GitHub | `oauth_state_invalid`, `github_token_exchange_failed`, `github_token_invalid` |
| Sync | `SYNC_SESSION_EXPIRED`, `SYNC_SESSION_NOT_FOUND`, `SYNC_SESSION_FORBIDDEN`, `SYNC_NO_CHANGES`, `SYNC_CONFLICT`, `SYNC_INVALID_ENTITY`, `SYNC_ENTITY_NOT_FOUND` |
| External services | `EXTERNAL_API_ERROR`, `EXTERNAL_FIRECRAWL_ERROR`, `EXTERNAL_OPENROUTER_ERROR`, `EXTERNAL_TELEGRAM_ERROR`, `EXTERNAL_SERVICE_TIMEOUT`, `EXTERNAL_SERVICE_UNAVAILABLE` |
| Internal processing | `INTERNAL_ERROR`, `DATABASE_ERROR`, `INTERNAL_DATABASE_ERROR`, `PROCESSING_ERROR`, `CONFIGURATION_ERROR`, `INTERNAL_CONFIG_ERROR`, `AUTH_SERVICE_UNAVAILABLE`, `FEATURE_DISABLED` |

This table groups codes for navigation; it is not a second enum. Check the two source modules and generated OpenAPI document before adding client-side exhaustive matching.

## Retry behavior

Retry only when `retryable` is true or the endpoint contract explicitly permits it. Honor `retry_after` when present. Validation, authentication, authorization, and conflict errors normally require input or state changes rather than an immediate retry.
