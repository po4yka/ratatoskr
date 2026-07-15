# Configure Twitter / X Extraction

Twitter/X URLs use the dedicated platform extractor in `app/adapters/twitter/`.
The coordinator can obtain a post through a connected user's X API token,
self-hosted Firecrawl, or an opt-in authenticated Playwright session.

## Supported URLs

- `x.com/<user>/status/<id>` and `twitter.com/<user>/status/<id>` posts;
- public or authenticated threads when the selected tier exposes the replies;
- X Article links resolved from path, redirect, or canonical metadata.

Profiles, searches, and hashtag pages are not summary-entry contracts.

## Extraction order

For a normal post, the coordinator runs:

```text
connected-user X API → self-hosted Firecrawl → optional Playwright
```

The X API stage is skipped when the request has no user, no active X connection,
or insufficient read scopes. X Articles skip the API stage. Firecrawl and
Playwright are then controlled by `TWITTER_FORCE_TIER`,
`TWITTER_PREFER_FIRECRAWL`, and `TWITTER_PLAYWRIGHT_ENABLED`.

`TWITTER_FORCE_TIER` selects only between the Firecrawl/Playwright fallback
tiers; it does not suppress a usable connected-user API stage.

## Base configuration

The checked-in defaults enable URL detection and the Firecrawl tier, while
leaving Playwright off:

```yaml
twitter:
  enabled: true
  prefer_firecrawl: true
  playwright_enabled: false
  force_tier: auto
```

The Firecrawl tier requires the self-hosted client configured by
`FIRECRAWL_SELF_HOSTED_ENABLED=true` and `FIRECRAWL_SELF_HOSTED_URL`. A cloud
Firecrawl API key is not an active extraction path.

Valid fallback tier modes are `auto`, `firecrawl`, and `playwright`.
Configuration validation rejects a forced tier whose required gate is disabled.

## Connected-user X API

Configure the OAuth client used by Mobile API social connection endpoints:

```bash
X_OAUTH_CLIENT_ID=...
X_OAUTH_CLIENT_SECRET=...
X_OAUTH_REDIRECT_URI=https://ratatoskr.example.com/v1/auth/x/callback
X_OAUTH_SCOPES='tweet.read users.read offline.access'
```

Write scopes are rejected. Tokens are stored through the social-connection
repository and resolved per requesting user. A 401 marks the connection as
requiring reauthentication; rate-limit and provider metadata are persisted in
the fetch attempt.

This stage calls X API v2 for an individual post. It is not a generic home-feed
or search extractor.

## Self-hosted Firecrawl

Enable the shared self-hosted client and keep Firecrawl preferred:

```bash
FIRECRAWL_SELF_HOSTED_ENABLED=true
FIRECRAWL_SELF_HOSTED_URL=http://firecrawl-api:3002
TWITTER_PREFER_FIRECRAWL=true
```

Start the scraper profile when using the bundled sidecar:

```bash
POSTGRES_PASSWORD=... \
docker compose -f ops/docker/docker-compose.yml \
  --profile with-scrapers up -d firecrawl-api
```

Login-wall/thin UI output is treated as a quality failure so the coordinator can
fall through instead of summarizing the sign-in page.

## Authenticated Playwright fallback

Playwright is useful for content visible to a browser session but not to the API
or Firecrawl. It is opt-in because the cookie file grants account access.

Local dependencies:

```bash
uv sync --extra browser_scraper
uv run playwright install chromium
```

Export a Netscape-format `cookies.txt` from an authenticated X session, store it
outside the repository, restrict its permissions, and mount it read-only at the
configured path.

```bash
TWITTER_PLAYWRIGHT_ENABLED=true
TWITTER_COOKIES_PATH=/data/twitter_cookies.txt
TWITTER_HEADLESS=true
TWITTER_PAGE_TIMEOUT_MS=15000
TWITTER_MAX_CONCURRENT_BROWSERS=2
```

`TWITTER_MAX_CONCURRENT_BROWSERS` accepts 1 through 20; increase it only after
measuring RAM and provider throttling. Never log or persist cookie contents.

## X Article resolution

`app/adapters/twitter/article_link_resolver.py` checks direct article paths,
redirect targets, and canonical links. It records one of:

- `path_match`;
- `redirect_match`;
- `canonical_match`;
- `not_article`;
- `resolve_failed`.

The resolver is controlled by
`TWITTER_ARTICLE_REDIRECT_RESOLUTION_ENABLED` and
`TWITTER_ARTICLE_RESOLUTION_TIMEOUT_SEC`. Leave it enabled unless the deployment
intentionally blocks the required public egress.

## Verify and troubleshoot

Send a known visible post and inspect the request's correlation ID. Persisted
metadata includes `tier_outcomes` for `x_api`, `firecrawl`, and `playwright`, plus
the selected `auth_strategy`.

An optional live diagnostic can exercise configured public URLs:

```bash
TWITTER_ARTICLE_LIVE_SMOKE_ENABLED=true \
uv run python tools/scripts/twitter_article_live_smoke.py
```

Common failures:

- `no_connection`/`skipped`: no usable connected-user X OAuth session;
- login-wall quality failure: enable a permitted fallback or authenticated
  Playwright;
- 401 from X API: reconnect the user's X account;
- Playwright 401/403: refresh the cookie export and verify its mount;
- 429: reduce browser concurrency/request volume and respect reset metadata;
- `resolve_failed`: inspect network/SSRF diagnostics for the resolver.

See [Troubleshooting](../reference/troubleshooting.md#content-extraction-failures)
and [Social Integrations](../reference/social-integrations.md) for connected-account
setup.
