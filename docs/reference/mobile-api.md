# Mobile API

The FastAPI application in `app/api/` is the backend contract for mobile, browser, and other HTTP clients. Generated OpenAPI files are the canonical machine-readable surface:

- `docs/openapi/mobile_api.yaml`
- `docs/openapi/mobile_api.json`

Do not edit those files by hand. Change routers or Pydantic models, then regenerate and validate the contract as described in [OpenAPI contract workflow](openapi-contract-workflow.md).

At the 2026-07-15 audit point, the generated contract contains 223 paths, including 218 under `/v1`, and 277 operations. These counts are a drift signal, not a compatibility promise.

## Authentication

The API supports the authentication modes implemented under `app/api/routers/auth/` and in `app/api/middleware.py`:

- bearer access tokens for authenticated API calls;
- refresh-token rotation through the auth endpoints;
- client-secret authentication where a route explicitly supports it;
- OAuth flows for supported external integrations;
- secure cookies for browser-facing auth flows where configured.

User and client allowlists are enforced by the backend configuration. A valid token does not bypass owner or client authorization rules.

## Response contract

Shared responses use the models in `app/api/models/responses/common.py`. Successful envelopes use `success: true`; errors use `success: false` with a stable string code and correlation metadata. See [API errors](api-error-codes.md).

Long-running request processing exposes progress as server-sent events. Request-specific streams are implemented in `app/api/routers/content/streams.py`; generic operation streams are implemented in `app/api/routers/operation_streams.py`. Clients must handle reconnects, terminal events, and authorization failures rather than assuming an uninterrupted connection.

## Route ownership

| Surface | Backend location |
|---|---|
| Summaries, requests, search, request streams | `app/api/routers/content/` |
| Authentication and token lifecycle | `app/api/routers/auth/` |
| User profile and user-owned data | `app/api/routers/user/` |
| Digest and signal endpoints | `app/api/routers/social/` |
| Collections | `app/api/routers/collections.py` |
| GitHub repositories | `app/api/routers/repositories.py` |
| Git mirrors | `app/api/routers/git_mirrors.py` |
| Synchronization | `app/api/routers/sync.py` and `app/api/services/sync/` |
| Generic operation streams | `app/api/routers/operation_streams.py` |

This table is an ownership map, not an endpoint inventory. Use generated OpenAPI for paths, methods, schemas, security requirements, and status codes.

## Compatibility workflow

```bash
make generate-openapi
make check-openapi-drift
make check-openapi-validate
make check-openapi
```

Commit router/model changes and regenerated YAML/JSON together. Client repositories should consume the generated contract rather than duplicating hand-written endpoint lists from this page.
