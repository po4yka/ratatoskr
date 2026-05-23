# Configure Optional Source Ingestors

Phase 5 adds optional proactive ingestors that emit generic `Source` and `FeedItem` rows for signal scoring. Generic RSS remains the default path. Hacker News and Reddit are zero-cost optional sources. Substack is an RSS specialization. The legacy generic X/Twitter placeholder remains disabled unless explicitly cost-acknowledged. Authenticated connected-account X and Threads feed ingestion is also disabled by default and requires explicit per-provider flags plus active social connections.

## Enable Hacker News

```yaml
signal_ingestion:
  enabled: true
  hn_enabled: true
  hn_feeds:
    - top
    - best
  max_items_per_source: 30
```

Supported HN feeds are `top`, `best`, `new`, and `newest`. The adapter uses `https://hacker-news.firebaseio.com/v0/*stories.json` plus `item/{id}.json`. No API key is required. Items are persisted as `kind=hacker_news` with score, comment count, author, URL, and timestamp metadata.

## Enable Reddit

```yaml
signal_ingestion:
  enabled: true
  reddit_enabled: true
  reddit_subreddits:
    - selfhosted
    - python
  reddit_listing: hot
  reddit_requests_per_minute: 60
  max_items_per_source: 25
```

The adapter uses public subreddit JSON endpoints such as `https://www.reddit.com/r/selfhosted/hot.json`. Credentials are not required for public subreddits. The default request budget is 60 requests/minute and config validation rejects values above 100 requests/minute. HTTP 429 is treated as a rate-limit error; HTTP 401/403 is treated as an auth/permission error and trips the source circuit breaker quickly.

## Enable Substack

Substack uses the same RSS path as normal feeds. Add the publication feed through the existing RSS subscription flow, or resolve the URL with `app.adapters.rss.substack.resolve_substack_feed_url`:

```text
platformer -> https://platformer.substack.com/feed
https://platformer.substack.com/p/post -> https://platformer.substack.com/feed
https://www.custom-domain.com -> https://www.custom-domain.com/feed
```

Substack feed rows are persisted as `kind=substack` while using the same `RssSignalIngester` contract as RSS.

## GitHub Repositories

GitHub repository ingestion is a pull-based ingestor: it reads repositories from a user's GitHub starred list on a daily schedule and optionally accepts on-demand ingest via the API or bot.

**An active GitHub integration is required.** There is no anonymous path; github.com URLs are rejected without a connected account.

### Connecting via PAT (preferred for headless or first-time setup)

Generate a token at https://github.com/settings/tokens/new with at least the `public_repo` scope (add `repo` for private repositories).

```http
POST /v1/auth/github/pat
Authorization: Bearer <jwt>
Content-Type: application/json

{ "token": "ghp_..." }
```

PAT auth does not require Redis or an OAuth App registration.

### Connecting via OAuth Device Flow (better UX for interactive setup)

The Device Flow is only available when `GITHUB_OAUTH_APP_CLIENT_ID` and `GITHUB_OAUTH_APP_CLIENT_SECRET` are set, and Redis is running.

1. Call `POST /v1/auth/github/device/start` -- the server returns `user_code` and `verification_uri`.
2. Display the `user_code` to the user; they visit `verification_uri` and authorize.
3. Poll `POST /v1/auth/github/device/poll` with `{ "device_code": "..." }` until `status: ok`.

### Daily sync schedule

The stars sync task runs at `0 2 * * *` UTC (`app/tasks/github_sync.py`). To trigger manually:

```bash
python -m app.cli.sync_github_stars --user-id <id>
```

### LLM concurrency and daily budget

Two env vars cap analysis cost:

```env
GITHUB_SYNC_LLM_CONCURRENCY=3       # parallel LLM calls during sync (default: 3)
GITHUB_SYNC_LLM_DAILY_BUDGET=50     # max repos analyzed per day per user (default: 50)
```

If the daily budget is reached, remaining repos are stored with `analysis=null` and `pending_analysis=true`. Run the next day's sync or use `--force-reanalyze` to clear the backlog.

## X/Twitter Cost Gate

Default installs never start X/Twitter ingestion. To opt in, both flags are required:

```yaml
signal_ingestion:
  enabled: true
  twitter_enabled: true
  twitter_ack_cost: true
```

The equivalent environment override is:

```env
TWITTER_INGESTION_ENABLED=true
TWITTER_INGESTION_ACK_COST=true
```

This is intentionally separate from `twitter.enabled`, which controls one-off X/Twitter URL extraction. Proactive X/Twitter polling is bring-your-own-token and has an explicit cost warning because the Basic tier is approximately $200/month.

## Authenticated Social Feeds

Connected-account social feed ingestion uses encrypted OAuth credentials from `social_connections`; it never runs unless global signal ingestion and the provider-specific social flag are enabled. X supports the authenticated user's post timeline by default and can be switched to the reverse-chronological home timeline with `SOCIAL_X_TIMELINE_MODE=home_timeline`; both modes require `tweet.read` and `users.read`. Threads uses the official Threads Graph `GET /me/threads` surface with `threads_basic`. Instagram proactive ingestion is intentionally not implemented.

```env
SIGNAL_INGESTION_ENABLED=true
SOCIAL_X_INGESTION_ENABLED=true
SOCIAL_THREADS_INGESTION_ENABLED=true
SOCIAL_X_TIMELINE_MODE=user_posts
```

The runner still applies the normal source controls: `SIGNAL_MAX_ITEMS_PER_SOURCE` limits provider fetch size, per-source `max_items_per_run` limits persistence, `backoff_until` skips sources until due, provider 429 reset headers are recorded as source backoff, and connections in `needs_reauth` are skipped without provider calls.
