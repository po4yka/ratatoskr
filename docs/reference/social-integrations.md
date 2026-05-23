# Social Integrations

## User-Facing Surfaces

Connected X, Instagram, and Threads accounts are managed through the Mobile API social-auth routes and through Telegram commands. In Telegram, `/social` lists token-safe provider status, `/connect_x`, `/connect_threads`, and `/connect_instagram` return OAuth connect URLs, and `/disconnect_social <provider>` deletes local token state for `x`, `threads`, or `instagram`.

## Optional Authenticated Feed Ingestion

Proactive connected-account feed ingestion is opt-in. X ingestion requires `SIGNAL_INGESTION_ENABLED=true`, `SOCIAL_X_INGESTION_ENABLED=true`, an active X connection, and `tweet.read` plus `users.read`; it supports `SOCIAL_X_TIMELINE_MODE=user_posts` for `GET /2/users/:id/tweets` or `SOCIAL_X_TIMELINE_MODE=home_timeline` for `GET /2/users/:id/timelines/reverse_chronological`. Threads ingestion requires `SIGNAL_INGESTION_ENABLED=true`, `SOCIAL_THREADS_INGESTION_ENABLED=true`, an active Threads connection, and `threads_basic`; it uses `GET /me/threads`. The source runner preserves standard source controls including `max_items_per_run`, `backoff_until`, rate-limit reset backoff, and `needs_reauth` skips. Instagram ingestion is not implemented.

Reference links: [X timelines](https://docs.x.com/x-api/posts/timelines/introduction), [X OAuth 2.0 endpoint mapping](https://docs.x.com/fundamentals/authentication/guides/v2-authentication-mapping), [Threads API](https://developers.facebook.com/docs/threads/).

## Provider Capabilities

Runtime capabilities are defined in `app/application/dto/social_capabilities.py`, exposed on `/v1/social/connections`, included in admin diagnostics, and enforced when `/v1/social/{provider}/connect-url` validates requested OAuth scopes. The current supported scope set is read-only: X uses `tweet.read`, `users.read`, and `offline.access`; Threads uses `threads_basic`; Instagram uses `instagram_business_basic`. Write/publish scopes are not requested.

| Provider | Single URL lookup | Owned media lookup | Public media lookup | Timeline ingestion | Refresh tokens | Notes |
|----------|-------------------|--------------------|---------------------|--------------------|----------------|-------|
| `x` | yes | no | yes | yes | yes | Read-only post lookup and configured timeline ingestion only; write, publish, DM, moderation, and ad scopes are unsupported. |
| `threads` | yes | yes | yes | yes | yes | Read-only media lookup and the connected account's `/me/threads` feed only; publishing, replies, insights, and webhooks are unsupported. |
| `instagram` | yes | yes | no | no | yes | Authenticated API lookup is only for media owned by the connected professional account; generalized public/private feed access is not promised. |

## Observability and Privacy Guardrails

Social integrations emit provider-level Prometheus counters from `app/observability/metrics.py`: `ratatoskr_social_fetch_total{provider,status,auth_tier}` for content fetch attempts, `ratatoskr_social_token_refresh_total{provider,status}` for OAuth refreshes, `ratatoskr_social_rate_limit_total{provider}` for provider 429s, and `ratatoskr_social_connection_status_total{provider,status}` for connection-state observations. Fetch counters are recorded at the `SocialFetchAttempt` persistence boundary so X, Threads, Instagram, and future social providers share the same telemetry contract.

`social_fetch_attempts.metadata_json` is intentionally not a raw provider payload store. The repository adapter keeps only safe operational keys: `api_status`, `api_supported_for_url`, `auth_strategy`, `connection_id`, `correlation_id`, `media_lookup_count`, `provider_resource_id`, `provider_shortcode`, `rate_limit`, `tweet_id`, and `unsupported_reason`. Nested metadata keys containing token, secret, authorization, cookie, code, or state markers are dropped. Raw provider responses, request headers, cookies, access tokens, refresh tokens, authorization codes, OAuth state values, and bearer credentials must not be persisted there.

Social auth and content-fetch logs preserve the standard structured logging format and include `cid`/correlation ID where one exists. `app/core/logging_utils.py` redacts social OAuth secrets in dictionaries, free text, URLs, and headers, including `access_token`, `refresh_token`, authorization `code`, OAuth `state`, cookies, and `Authorization` header values.

## Instagram API Scaffold

This project currently keeps unsupported public Instagram URL summarization on the existing unauthenticated Meta scraper fallback. The authenticated Instagram API tier is wired only for supported connected-account media: it enumerates the connected professional account's media IDs and fetches a matching owned media object. It does not promise generalized public media lookup, private feed access, or personal-account media access.

Docs verified on 2026-05-23 against Meta's Instagram Platform documentation:

- Auth flow: Instagram API with Instagram Login uses Business Login for Instagram. The authorization endpoint is `https://www.instagram.com/oauth/authorize`, the short-lived token exchange endpoint is `https://api.instagram.com/oauth/access_token`, the long-lived token exchange endpoint is `https://graph.instagram.com/access_token`, and the long-lived token refresh endpoint is `https://graph.instagram.com/refresh_access_token`.
- Scope model: the read-only scaffold requests `instagram_business_basic`. Meta documents additional scopes such as `instagram_business_content_publish`, `instagram_business_manage_messages`, and `instagram_business_manage_comments`, but this project does not request or implement behavior for those scopes.
- Account restrictions: Instagram API with Instagram Login is for Instagram professional accounts, businesses and creators. Meta's media reference states that media reads return only data for media owned by Instagram professional accounts and cannot be used for media owned by personal Instagram accounts.
- Supported read endpoints in this scaffold: `GET /me` for the connected professional account profile, `GET /<IG_ID>/media` for IDs of that account's media objects, and `GET /<IG_MEDIA_ID>` for fields on a specific Instagram media object.
- Token lifetime: Business Login returns a short-lived Instagram User access token. The scaffold exchanges it for a long-lived token; Meta documents long-lived tokens as valid for about 60 days. A long-lived token can be refreshed for another 60 days when it is at least 24 hours old, unexpired, and the app user granted `instagram_business_basic`.

Explicitly unsupported:

- No username/password automation and no logged-in Instagram page scraping.
- No cookie storage as primary auth.
- No private feed access, private post access, or personal-account media access claims.
- No competitor/public-feed scraping through authenticated Instagram APIs.
- No publishing, comment moderation, messaging, insights, ads, tagging, or webhook behavior in this scaffold.

Reference links:

- [Instagram API with Instagram Login](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/)
- [Business Login for Instagram](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/business-login/)
- [Get Started with Instagram API with Instagram Login](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/get-started/)
- [IG Media reference](https://developers.facebook.com/docs/instagram-platform/reference/instagram-media)
- [Refresh Access Token reference](https://developers.facebook.com/docs/instagram-platform/reference/refresh_access_token)
