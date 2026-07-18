# Build Your First Mobile API Client

This guide covers the smallest supported flow for a native client: authenticate,
call an authorized endpoint, rotate the refresh token, and then adopt the sync
contract. The committed OpenAPI files remain the source of truth for request and
response schemas:

- [`docs/openapi/mobile_api.yaml`](../openapi/mobile_api.yaml)
- [`docs/openapi/mobile_api.json`](../openapi/mobile_api.json)

The Compose deployment publishes the API at `http://127.0.0.1:18000` by default.

## 1. Check the API

```bash
curl --fail http://127.0.0.1:18000/health
```

If the API is not running, follow [Quickstart](quickstart.md) or
[Production Deployment](deploy-production.md). Do not infer readiness from the
container state alone: the health request must succeed.

## 2. Choose an authentication flow

Ratatoskr supports several authentication endpoints. Native clients normally use
one of these:

- `POST /v1/auth/telegram-login` with data signed by the Telegram Login Widget or
  Telegram Web App;
- `POST /v1/auth/credentials-login` where credential login is configured;
- `POST /v1/auth/secret-login` for a provisioned client secret.

There is no bot `/mobile_login` command or one-time Telegram token exchange. The
Telegram endpoint verifies the genuine Telegram signature. Its JSON body uses
the Telegram field names plus a Ratatoskr client identifier:

```json
{
  "id": 123456789,
  "hash": "telegram-provided-hmac",
  "auth_date": 1784088000,
  "username": "alice",
  "first_name": "Alice",
  "last_name": null,
  "photo_url": null,
  "client_id": "ios-app-v1.0"
}
```

`client_id` may contain letters, digits, `.`, `_`, and `-`, and must be allowed
by the server authentication configuration. Never manufacture or modify the
Telegram-signed fields in a client.

Successful API responses use the common envelope. A non-web client receives the
tokens under `data.tokens`:

```json
{
  "success": true,
  "data": {
    "tokens": {
      "accessToken": "...",
      "refreshToken": "...",
      "expiresIn": 900,
      "tokenType": "Bearer"
    },
    "sessionId": 42
  },
  "meta": {
    "correlationId": "...",
    "version": "1.0.0"
  }
}
```

The exact expiry is server configuration, not a client constant. Web clients
receive the refresh token in an `HttpOnly` cookie instead of the JSON body.

## 3. Make an authorized request

The following Python example accepts an already signed Telegram payload. It does
not implement Telegram UI integration.

```python
from typing import Any

import httpx


class RatatoskrClient:
    def __init__(self, base_url: str = "http://127.0.0.1:18000") -> None:
        self.http = httpx.Client(base_url=base_url, timeout=20.0)
        self.access_token: str | None = None
        self.refresh_token: str | None = None

    def telegram_login(self, signed_payload: dict[str, Any]) -> None:
        response = self.http.post("/v1/auth/telegram-login", json=signed_payload)
        response.raise_for_status()
        tokens = response.json()["data"]["tokens"]
        self.access_token = tokens["accessToken"]
        self.refresh_token = tokens["refreshToken"]

    def list_summaries(self, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        if self.access_token is None:
            raise RuntimeError("authenticate before calling the API")
        response = self.http.get(
            "/v1/summaries",
            params={"limit": limit, "offset": offset},
            headers={"Authorization": f"Bearer {self.access_token}"},
        )
        response.raise_for_status()
        return response.json()

    def refresh(self) -> None:
        if self.refresh_token is None:
            raise RuntimeError("no refresh token is available")
        response = self.http.post(
            "/v1/auth/refresh",
            json={"refresh_token": self.refresh_token},
        )
        response.raise_for_status()
        tokens = response.json()["data"]["tokens"]
        self.access_token = tokens["accessToken"]
        self.refresh_token = tokens["refreshToken"]
```

Store refresh tokens in the platform secure store (Keychain on Apple platforms,
Keystore-backed encrypted storage on Android). Keep access tokens in memory where
practical. Do not log either token.

Every protected request sends:

```http
Authorization: Bearer <access-token>
```

Errors also use the common envelope and include `meta.correlationId`. Preserve
that identifier in client diagnostics; it is the server-side lookup key for a
failed request.

## 4. Rotate, do not reuse, refresh tokens

`POST /v1/auth/refresh` rotates the refresh token. Replace the stored token only
after a successful response and never send the previous token again. Reuse can
revoke the whole token family as a theft precaution.

Use `POST /v1/auth/logout` to revoke the current session and
`POST /v1/auth/logout-all` to revoke every session for the authenticated user.
See [Mobile API](../reference/mobile-api.md#authentication) for the complete
authentication policy.

## 5. Add offline sync

Do not invent a client-side merge protocol from the summaries endpoints. The v2
sync contract defines cursors, mutations, conflicts, and tombstones. Generate
request/response types from OpenAPI, then implement the state machine documented
in [Sync Protocol](../reference/sync-protocol.md).

For long-running saves, consume the request-specific server-sent-event endpoint
defined in OpenAPI instead of polling undocumented fields.

## Completion checklist

- `/health` succeeds on the intended deployment.
- The client uses an authentication method enabled by that deployment.
- Telegram login data comes directly from Telegram and passes signature checks.
- `client_id` is stable and allowed by the server.
- Bearer authentication can fetch `/v1/summaries`.
- Refresh-token rotation replaces the stored token atomically.
- Tokens are kept out of logs and insecure application storage.
- Generated API types and the v2 sync protocol are used for broader integration.
