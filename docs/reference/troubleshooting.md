# Troubleshooting Guide

This guide helps you diagnose and resolve common issues with Ratatoskr.

## Table of Contents

- [Debugging with Correlation IDs](#debugging-with-correlation-ids)
- [Installation Issues](#installation-issues)
- [Configuration Issues](#configuration-issues)
- [Content Extraction / Scraper Chain Issues](#content-extraction--scraper-chain-issues)
- [OpenRouter Issues](#openrouter-issues)
- [YouTube Issues](#youtube-issues)
- [Database Issues](#database-issues)
- [Redis Issues](#redis-issues)
- [Qdrant Issues](#qdrant-issues)
- [Mobile API Issues](#mobile-api-issues)
- [External Aggregation and Auth Issues](#external-aggregation-and-auth-issues)
- [MCP Server Issues](#mcp-server-issues)
- [Performance Issues](#performance-issues)
- [Debugging Strategies](#debugging-strategies)

---

## Debugging with Correlation IDs

**Correlation IDs are your best debugging tool.** Every request in Ratatoskr gets a unique `correlation_id` that ties together:

- Telegram messages
- Database requests
- Scraper chain provider calls
- OpenRouter LLM calls
- Log entries

### How to Find Correlation IDs

1. **From Error Messages**: All user-facing errors include `Error ID: <correlation_id>`

   ```
   ❌ Failed to summarize article.
   Error ID: a1b2c3d4-e5f6-g7h8-i9j0-k1l2m3n4o5p6
   ```

2. **From Logs**: Search logs for the error message, find the correlation_id

   ```bash
   grep "a1b2c3d4" /var/log/ratatoskr/app.log
   ```

3. **From Database**: Query the `requests` table

   ```sql
   SELECT * FROM requests WHERE id = 'a1b2c3d4-e5f6-g7h8-i9j0-k1l2m3n4o5p6';
   ```

### Using Correlation IDs

Once you have a correlation ID:

```sql
-- See the full request details
SELECT * FROM requests WHERE id = '<correlation_id>';

-- See Firecrawl response
SELECT * FROM crawl_results WHERE request_id = '<correlation_id>';

-- See LLM calls (prompt, response, errors)
SELECT * FROM llm_calls WHERE request_id = '<correlation_id>';

-- See final summary
SELECT * FROM summaries WHERE request_id = '<correlation_id>';

-- See Telegram messages
SELECT * FROM telegram_messages WHERE request_id = '<correlation_id>';

-- See normalized latest failure snapshot (if request failed)
SELECT id, status, error_type, error_message, error_context_json
FROM requests
WHERE id = '<correlation_id>';
```

Common `error_context_json.reason_code` values:

- `SCRAPER_CHAIN_EXHAUSTED` -- all providers failed
- `FIRECRAWL_ERROR` (self-hosted)
- `FIRECRAWL_LOW_VALUE`
- `CRAWL4AI_ERROR`
- `DEFUDDLE_ERROR`
- `PLAYWRIGHT_EMPTY_CONTENT`
- `PLAYWRIGHT_UI_OR_LOGIN`
- `RESOLVE_FAILED`
- `EXTRACTION_EMPTY_OUTPUT`

**Pro Tip**: `DEBUG_PAYLOADS=1` enables bounded debug previews only. Authorization headers, provider tokens, prompts, raw source content, and private URL path/query data remain redacted by default; use it only in controlled local debugging.

---

## Installation Issues

### Python Version Mismatch

**Symptom**: `ImportError` or syntax errors when running the bot.

**Cause**: Python 3.13+ required, older version installed.

**Solution**:

```bash
python3 --version  # Should be 3.13 or higher
# If not, install Python 3.13+ and recreate venv
pyenv install 3.13.0
pyenv local 3.13.0
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Missing ffmpeg

**Symptom**: YouTube downloads fail with `ffmpeg not found`.

**Cause**: yt-dlp requires ffmpeg for video/audio merging.

**Solution**:

```bash
# macOS
brew install ffmpeg

# Ubuntu/Debian
sudo apt-get update && sudo apt-get install -y ffmpeg

# Verify
ffmpeg -version
```

### Dependency Installation Failures

**Symptom**: `pip install` fails with compilation errors (especially on ARM/M1 Macs).

**Cause**: Some packages (like qdrant-client, sentence-transformers) require system libraries.

**Solution**:

```bash
# macOS (M1/M2)
brew install cmake pkg-config

# Ubuntu/Debian
sudo apt-get install -y build-essential python3-dev

# Then retry
pip install -r requirements.txt
```

### Pre-commit Hook Failures

**Symptom**: `git commit` fails with pre-commit errors.

**Cause**: Code doesn't pass ruff formatting or mypy type checks.

**Solution**:

```bash
# Auto-fix formatting issues
make format

# Check what's still failing
make lint
make type

# If you need to bypass hooks temporarily (NOT recommended)
git commit --no-verify
```

---

## Configuration Issues

### Missing Required Environment Variables

**Symptom**: Bot fails to start with `KeyError` or `ValidationError`.

**Cause**: Required env vars not set in `.env` file.

**Solution**:

```bash
# Check which vars are missing
python -c "from app.config.settings import RuntimeConfig; RuntimeConfig()"

# Add missing vars to .env
cat >> .env << EOF
API_ID=your_api_id
API_HASH=your_api_hash
BOT_TOKEN=your_bot_token
ALLOWED_USER_IDS=123456789
OPENROUTER_API_KEY=your_key
# No FIRECRAWL_API_KEY needed -- self-hosted sidecars replace cloud Firecrawl
EOF
```

See [environment_variables.md](environment-variables.md) for full reference.

> **Breaking scraper rename**: startup now fails if legacy vars are present (`SCRAPLING_ENABLED`, `SCRAPLING_TIMEOUT_SEC`, `SCRAPLING_STEALTH_FALLBACK`, `SCRAPER_DIRECT_HTTP_ENABLED`). Use the new `SCRAPER_*` names from `docs/reference/environment-variables.md`.

### Invalid API Keys

**Symptom**: Bot starts but all summaries fail with "401 Unauthorized" or "Invalid API key".

**Cause**: Expired, revoked, or mistyped API keys.

**Solution**:

```bash
# Test self-hosted Firecrawl sidecar (no API key needed)
curl -X POST http://localhost:3002/v1/scrape \
     -H "Content-Type: application/json" \
     -d '{"url":"https://example.com"}'

# Test OpenRouter key
curl -H "Authorization: Bearer $OPENROUTER_API_KEY" \
     https://openrouter.ai/api/v1/models

# If OpenRouter key is invalid, regenerate at:
# - OpenRouter: https://openrouter.ai/keys
```

### Access Denied (User Not Whitelisted)

**Symptom**: Bot replies "Access denied" when you message it.

**Cause**: Your Telegram user ID not in `ALLOWED_USER_IDS`.

**Solution**:

```bash
# Find your Telegram user ID
# Method 1: Message @userinfobot on Telegram

# Method 2: Check bot logs when you message it
grep "Access denied" /var/log/ratatoskr/app.log
# Look for: "user_id": 987654321

# Add to .env
echo "ALLOWED_USER_IDS=123456789,987654321" >> .env

# Restart bot
docker restart ratatoskr
```

---

## Content Extraction / Scraper Chain Issues

> **Multi-provider fallback**: Content extraction uses an ordered chain of providers (default: Scrapling → Crawl4AI → Firecrawl self-hosted → Defuddle → Playwright → Crawlee → direct HTML → ScrapeGraphAI). If one provider fails, the next is tried automatically. Cloud Firecrawl is not used; `FIRECRAWL_API_KEY` is not required. The self-hosted sidecar stack (Firecrawl, Crawl4AI, Defuddle) is started with `--profile with-scrapers`. See `docs/reference/environment-variables.md` for scraper chain configuration.

### Self-Hosted Sidecar Rate / Capacity Issues

**Symptom**: "429 Too Many Requests" or slow responses from a scraper sidecar.

**Cause**: Self-hosted sidecar container is overloaded or under-resourced.

**Solution**:

```bash
# Check sidecar health
curl http://localhost:3002/health   # Firecrawl
curl http://localhost:11235/health  # Crawl4AI
curl http://localhost:3003/health   # Defuddle

# Restart the affected sidecar
docker compose -f ops/docker/docker-compose.yml --profile with-scrapers restart firecrawl-api
```

### Firecrawl Timeouts

**Symptom**: Summaries fail with "Timeout waiting for Firecrawl response".

**Cause**: Slow websites or Firecrawl server overload.

**Solution**:

```bash
# Increase self-hosted scraper Firecrawl timeout (default: 90s)
echo "SCRAPER_FIRECRAWL_TIMEOUT_SEC=120" >> .env

# Restart bot
docker restart ratatoskr
```

### Proxy Failures

**Symptom**: Firecrawl returns "Failed to fetch" for specific sites.

**Cause**: Site blocked Firecrawl's proxies or requires authentication.

**Solution**:

1. **Check if site is paywalled**: WSJ, NYT, Medium (members-only) fail even with Firecrawl
2. **Try different proxy**: Firecrawl rotates automatically, retry may work
3. **Fallback to trafilatura**: Set `CONTENT_EXTRACTION_FALLBACK=true` to use local extraction

### Content Extraction Failures

**Symptom**: Summary says "No content extracted" or "Article too short".

**Cause**: Firecrawl returned HTML but no clean markdown (e.g., SPAs, JavaScript errors).

**Solution**:

```bash
# Enable bounded Firecrawl payload previews; raw content and private URLs stay redacted
echo "DEBUG_PAYLOADS=1" >> .env

# Check database for Firecrawl response
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT * FROM crawl_results
     WHERE request_id = (SELECT id FROM requests WHERE correlation_id = '<correlation_id>');"

# If Firecrawl failed, enable fallback
echo "CONTENT_EXTRACTION_FALLBACK=true" >> .env
```

---

### All Providers Failed (Scraper Chain Exhausted)

**Symptom**: Summary fails; logs contain `scraper_chain_exhausted` or reason code `SCRAPER_CHAIN_EXHAUSTED`.

**Cause**: All enabled scraper chain providers returned empty content or an error for this URL.

**Solution**:

```bash
# 1. Check which providers are enabled
grep SCRAPER_ .env

# 2. Verify sidecar health
curl http://localhost:3002/health      # self-hosted Firecrawl
curl http://localhost:11235/health     # Crawl4AI
curl http://localhost:3003/health      # Defuddle

# 3. Force a single provider for testing
echo "SCRAPER_FORCE_PROVIDER=scrapling" >> .env
# Valid values: scrapling | crawl4ai | firecrawl_self_hosted | defuddle | playwright | crawlee | direct_html | scrapegraph_ai

# 4. Check per-provider failure reasons in logs
docker logs ratatoskr 2>&1 | grep '"context":"scraper"'

# 5. Restart sidecars if unresponsive
docker compose -f ops/docker/docker-compose.yml --profile with-scrapers restart
```

**Last resort**: enable ScrapeGraphAI (`SCRAPER_SCRAPEGRAPH_ENABLED=true`) — uses an LLM call and adds latency/cost but handles sites that defeat all browser-based approaches.


## OpenRouter Issues

### Model Selection Errors

**Symptom**: Summaries fail with "Model not found" or "Model is offline".

**Cause**: Specified model unavailable or deprecated.

**Solution**:

```bash
# Check available models
curl https://openrouter.ai/api/v1/models | jq '.data[] | {id, name}'

# Update to working model
echo "OPENROUTER_MODEL=deepseek/deepseek-v4-flash" >> .env
echo "OPENROUTER_FALLBACK_MODELS=qwen/qwen3-max,moonshotai/kimi-k2.5" >> .env

# Restart bot
docker restart ratatoskr
```

### Rate Limiting

**Symptom**: "429 Rate Limit Exceeded" errors.

**Cause**: Too many concurrent requests or exceeded daily quota.

**Solution**:

```bash
# Reduce concurrency
echo "MAX_CONCURRENT_CALLS=2" >> .env  # Default: 4

# Add rate limit delay
echo "RATE_LIMIT_WINDOW_SECONDS=60" >> .env

# Check OpenRouter dashboard for usage
# https://openrouter.ai/account
```

### Token Limit Exceeded

**Symptom**: Summaries fail with "Token limit exceeded" or "Context length exceeded".

**Cause**: Article too long for model's context window.

**Solution**:

```bash
# Use long-context model
echo "OPENROUTER_LONG_CONTEXT_MODEL=moonshotai/kimi-k2.5" >> .env  # 256k context

# Or enable chunking (splits long articles)
echo "CHUNKING_ENABLED=true" >> .env
echo "CHUNK_MAX_CHARS=150000" >> .env

# Restart bot
docker restart ratatoskr
```

### Fallback Chain Failures

**Symptom**: "All models failed" error after trying fallbacks.

**Cause**: Primary and all fallback models failed (offline, rate-limited, or broken).

**Solution**:

```bash
# Check logs for specific model errors
grep "model failed" /var/log/ratatoskr/app.log

# Update fallback chain to reliable models
echo "OPENROUTER_FALLBACK_MODELS=qwen/qwen3-max,google/gemini-2.0-flash-001:free" >> .env

# Verify models are online
curl https://openrouter.ai/api/v1/models | jq '.data[] | select(.id | contains("qwen")) | {id, pricing}'
```

### JSON Parsing Failures

**Symptom**: "Failed to parse summary JSON" even after retries.

**Cause**: Model producing invalid JSON or missing required fields.

**Solution**:

```bash
# Enable JSON repair fallback
echo "ENABLE_JSON_REPAIR=true" >> .env

# Try different model (some models better at JSON)
echo "OPENROUTER_MODEL=qwen/qwen3-max" >> .env  # Qwen is excellent at JSON

# Enable structured outputs (if model supports)
echo "OPENROUTER_ENABLE_STRUCTURED_OUTPUTS=true" >> .env

# Check actual LLM response in database
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT response_json FROM llm_calls
     WHERE request_id = (SELECT id FROM requests WHERE correlation_id = '<correlation_id>')
     ORDER BY attempt_index;"
```

---

## YouTube Issues

### yt-dlp Not Found

**Symptom**: YouTube downloads fail with "yt-dlp not found".

**Cause**: yt-dlp not installed.

**Solution**:

```bash
pip install yt-dlp

# Or via system package manager
# macOS
brew install yt-dlp

# Ubuntu/Debian
sudo curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp
sudo chmod a+rx /usr/local/bin/yt-dlp
```

### Transcript Unavailable

**Symptom**: "No transcript available for this video".

**Cause**: Video lacks auto-generated or manual captions.

**Solution**:

- YouTube only: Use audio transcription (requires `WHISPER_API_KEY` or local Whisper)
- Or: enable the download fallback path so the YouTube pipeline can try downloaded subtitle/VTT fallback after transcript lookup

```bash
# Option 1: Enable Whisper transcription (if available)
echo "ENABLE_WHISPER_TRANSCRIPTION=true" >> .env
echo "WHISPER_API_KEY=your_key" >> .env

# Option 2: Skip video if no transcript
# (Default behavior: fails gracefully with error message)
```

### Storage Quota Exceeded

**Symptom**: YouTube downloads fail with "Disk full" or "No space left on device".

**Cause**: Downloaded videos fill up `YOUTUBE_DOWNLOAD_PATH` directory.

**Solution**:

```bash
# Check disk usage
du -sh /data/youtube_downloads/

# Clean old downloads
find /data/youtube_downloads/ -type f -mtime +7 -delete

# Or configure auto-cleanup
echo "YOUTUBE_CLEANUP_AFTER_DAYS=7" >> .env  # Delete after 7 days
echo "YOUTUBE_MAX_STORAGE_GB=10" >> .env    # Max 10 GB storage

# Restart bot
docker restart ratatoskr
```

### Format/Quality Issues

**Symptom**: Downloaded video has poor quality or wrong format.

**Cause**: Default format selection doesn't match availability.

**Solution**:

```bash
# Force 1080p (default)
echo "YOUTUBE_VIDEO_QUALITY=1080" >> .env

# Or accept lower quality if 1080p unavailable
echo "YOUTUBE_VIDEO_QUALITY=720" >> .env

# Change format
echo "YOUTUBE_VIDEO_FORMAT=mp4" >> .env  # Default: mp4

# Restart bot
docker restart ratatoskr
```

---

## Database Issues

### Database Locked

**Symptom**: "Database is locked" errors during writes.

**Cause**: Multiple processes accessing SQLite concurrently (not supported well).

**Solution**:

```bash
# Increase the asyncpg statement timeout (default 30 s)
echo "DB_STATEMENT_TIMEOUT_SEC=60" >> .env

# Inspect long-running queries
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT pid, age(now(), query_start) AS age, state, left(query, 80) AS query
     FROM pg_stat_activity
    WHERE datname = 'ratatoskr' AND state = 'active'
    ORDER BY age DESC;"
```

### Corruption

**Symptom**: Postgres reports `data corruption` or `invalid page header`, or pg\_dump fails on a relation.

**Cause**: Disk failure, ungraceful shutdown of the `ratatoskr-postgres` container, or filesystem damage on the `postgres_data` volume.

**Solution**:

```bash
# Inspect Postgres server logs
docker logs ratatoskr-postgres --tail 200

# If the database is unreachable, follow the standard PostgreSQL recovery
# sequence (verify the volume, REINDEX as needed) and then restore the most
# recent pg_dump:
docker exec -i ratatoskr-postgres \
  pg_restore --no-owner --no-privileges --clean --if-exists \
             -U ratatoskr_app -d ratatoskr \
  < backups/<timestamp>/ratatoskr.dump
```

**Prevention**: Enable automatic backups:

```bash
echo "DB_AUTO_BACKUP=true" >> .env
echo "DB_BACKUP_INTERVAL_HOURS=24" >> .env
```

### Migration Failures

**Symptom**: Bot fails to start after update with "Schema version mismatch".

**Cause**: Database schema out of date.

**Solution**:

```bash
# Run migrations
python -m app.cli.migrate_db

# Or force recreate (WARNING: deletes all data)
docker exec -i ratatoskr-postgres psql -U postgres -c "DROP DATABASE IF EXISTS ratatoskr;"
docker exec -i ratatoskr-postgres psql -U postgres -c "CREATE DATABASE ratatoskr OWNER ratatoskr_app;"
python -m app.cli.migrate_db

# Restore data from backup if needed
docker exec -i ratatoskr-postgres \
  pg_restore --no-owner --no-privileges --clean --if-exists \
             -U ratatoskr_app -d ratatoskr \
  < backups/<timestamp>/ratatoskr.dump
```

### Performance Issues

**Symptom**: Slow queries, high CPU usage from database.

**Cause**: Missing indexes or large tables.

**Solution**:

```bash
# Vacuum database (reclaim space, refresh planner stats)
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c "VACUUM (ANALYZE);"

# Analyze query performance
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "EXPLAIN ANALYZE
     SELECT * FROM summaries
      WHERE request_id = (SELECT id FROM requests WHERE input_url = 'https://example.com');"
```

---

## Redis Issues

### Connection Failures

**Symptom**: "Failed to connect to Redis" warnings.

**Cause**: Redis not running or wrong connection settings.

**Solution**:

```bash
# Check if Redis is running
redis-cli ping
# Should return: PONG

# If not running, start Redis
# macOS
brew services start redis

# Ubuntu/Debian
sudo systemctl start redis

# Docker
docker run -d -p 6379:6379 redis:7-alpine

# Update connection settings
echo "REDIS_URL=redis://localhost:6379/0" >> .env
echo "REDIS_TIMEOUT=5" >> .env

# Restart bot
docker restart ratatoskr
```

### Graceful Degradation

**Symptom**: Bot works but logs Redis errors.

**Cause**: Redis optional (caching only), bot continues without it.

**Solution**:

- **If Redis not needed**: Disable it entirely

  ```bash
  echo "REDIS_ENABLED=false" >> .env
  ```

- **If needed**: Fix connection (see above)

### Cache Invalidation

**Symptom**: Stale data returned from cache.

**Cause**: Cache not invalidated after updates.

**Solution**:

```bash
# Flush all cache
redis-cli FLUSHALL

# Or flush specific keys
redis-cli KEYS "summary:*" | xargs redis-cli DEL

# Adjust cache TTL
echo "REDIS_LLM_TTL_SECONDS=3600" >> .env  # Default: 1 hour
```

---

## Qdrant Issues

### Connection Failures

**Symptom**: Search fails with "Failed to connect to Qdrant".

**Cause**: Qdrant server not running or wrong URL.

**Solution**:

```bash
# Check if Qdrant is running
curl http://localhost:6333/healthz

# If not, start Qdrant
# Docker
docker run -d -p 6333:6333 -p 6334:6334 qdrant/qdrant:v1.12.4

# Or via compose
docker compose -f ops/docker/docker-compose.yml up -d qdrant

# Update connection settings
echo "QDRANT_URL=http://localhost:6333" >> .env

# Restart bot
docker restart ratatoskr
```

### Embedding Errors

**Symptom**: Search fails with "Failed to generate embeddings".

**Cause**: Sentence-transformers model not downloaded.

**Solution**:

```bash
# Download embedding model manually
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

# Restart bot
docker restart ratatoskr
```

### Collection Not Found

**Symptom**: "Collection 'summaries' does not exist".

**Cause**: Qdrant database not initialized or wiped.

**Solution**:

```bash
# Recreate collection and backfill embeddings
python -m app.cli.backfill_vector_store

# Check collections
curl http://localhost:6333/collections

# Verify count
curl http://localhost:6333/collections/summaries
```

---

## Mobile API Issues

### JWT Authentication Errors

**Symptom**: "Invalid token" or "Token expired" errors.

**Cause**: Expired JWT token or mismatched secret.

**Solution**:

```bash
# Verify JWT_SECRET is set
grep JWT_SECRET .env

# If missing, generate new secret
openssl rand -hex 32
echo "JWT_SECRET_KEY=<generated_secret>" >> .env

# Restart API
docker restart ratatoskr

# Client: Re-authenticate to get new token
curl -X POST http://localhost:8000/v1/auth/telegram-login \
     -H "Content-Type: application/json" \
     -d '{"telegram_user_id": 123456789, "telegram_auth_token": "..."}'
```

### Request Stuck In Processing

**Symptom**: A submitted URL remains `pending` or `processing`, the web SubmitPage keeps polling, or the SSE stream never reaches `done` / `error`.

**First checks**:

```bash
# Find the request and durable processing job by correlation/request id
grep "<correlation_id>" /var/log/ratatoskr/app.log

# Reproduce with the CLI runner and debug logs
python -m app.cli.summary --url <URL> --log-level DEBUG
```

**Likely owners**: `app/adapters/content/url_processor.py`, `app/adapters/content/platform_extraction/lifecycle.py`, `app/adapters/content/streaming/`, and `app/db/models/core.py::RequestProcessingJob`.

### Sync Conflicts

**Symptom**: "Sync conflict detected" errors during sync.

**Cause**: Client and server modified same data, conflict resolution failed.

**Solution**:

```bash
# Enable conflict logging
echo "SYNC_CONFLICT_LOGGING=debug" >> .env

# Check logs for conflict details
grep "sync conflict" /var/log/ratatoskr/app.log

# Client: Force full sync (discards local changes)
curl -X POST http://localhost:8000/v1/sync/summaries?mode=full \
     -H "Authorization: Bearer <token>"
```

### Rate Limiting

**Symptom**: "429 Too Many Requests" from mobile API.

**Cause**: Exceeded API rate limits (default: 100 req/min per user).

**Solution**:

```bash
# Increase rate limit
echo "API_RATE_LIMIT_DEFAULT=200" >> .env

# Or disable rate limiting (not recommended for production)
echo "API_ENABLE_RATE_LIMIT=false" >> .env

# Restart API
docker restart ratatoskr
```

---

## External Aggregation and Auth Issues

### Secret Login Fails

**Symptom**: `ratatoskr login` fails or `POST /v1/auth/secret-login` returns `401` or `403`.

**Cause**: One of the following is usually true:

- `SECRET_LOGIN_ENABLED` is disabled
- the submitted `client_id` is not in `ALLOWED_CLIENT_IDS`
- the plaintext secret was mistyped, rotated, or revoked
- too many failed attempts locked the secret temporarily

**Solution**:

```bash
# Server-side checks
grep SECRET_LOGIN_ENABLED .env
grep ALLOWED_CLIENT_IDS .env
grep SECRET_LOGIN_MAX_FAILED_ATTEMPTS .env
grep SECRET_LOGIN_LOCKOUT_MINUTES .env

# Retry the exchange directly
curl -X POST http://localhost:8000/v1/auth/secret-login \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": 123456,
    "client_id": "cli-workstation-v1",
    "secret": "<plaintext-secret>"
  }'
```

If the secret was rotated or revoked, mint a new one and retry. Plaintext client secrets are only available at create or rotate time.

### Refresh Token Stops Working

**Symptom**: CLI or custom client starts getting `401` on refresh after working earlier.

**Cause**:

- the refresh token was revoked explicitly
- the session was logged out
- refresh-token reuse detection revoked the wider session set

**Solution**:

```bash
# Try one refresh explicitly
curl -X POST http://localhost:8000/v1/auth/refresh \
  -H "Content-Type: application/json" \
  -d '{"refresh_token": "<refresh-token>"}'
```

If refresh fails, re-run `ratatoskr login` or repeat `secret-login` with an active client secret.

### Aggregation Create Is Denied Before Execution Starts

**Symptom**: `POST /v1/aggregations` returns `403` or `404` before any extraction begins.

**Cause**:

- `AGGREGATION_BUNDLE_ENABLED=false`
- the current `AGGREGATION_ROLLOUT_STAGE` does not include the caller
- the authenticated user is outside `ALLOWED_USER_IDS` for the current rollout phase

**Solution**:

```bash
grep AGGREGATION_BUNDLE_ENABLED .env
grep AGGREGATION_ROLLOUT_STAGE .env
grep ALLOWED_USER_IDS .env
```

Use `enabled` only after validating internal or beta rollout first.

### Aggregation URL Is Rejected as Unsupported or Unsafe

**Symptom**: Aggregation create returns `422` with validation details, or the server logs `aggregation.bundle_create_blocked_ssrf`.

**Cause**:

- the URL is not part of the public URL-first contract
- the URL points at localhost, a private network, or another blocked address range

**Solution**:

- resubmit public `http://` or `https://` URLs only
- remove internal, localhost, or VPN-only targets
- add `source_kind_hint` only when it matches one of the supported public hint values

### Aggregation Create Returns 429

**Symptom**: External bundle submission is rate limited even though other API calls still work.

**Cause**: Aggregation create has its own per-user and per-client guardrails.

**Solution**:

```bash
grep API_RATE_LIMIT_AGGREGATION_CREATE_USER .env
grep API_RATE_LIMIT_AGGREGATION_CREATE_CLIENT .env
```

Raise those limits carefully and monitor for spikes, because `/v1/aggregations` is much heavier than read endpoints.

### Aggregation Session Gets Stuck in Partial or Failed

**Symptom**: `ratatoskr aggregation get` or `GET /v1/aggregations/{id}` never reaches a clean `completed` state.

**Cause**:

- one or more upstream sources failed extraction
- synthesis finished with incomplete coverage
- a long-running upstream dependency exceeded practical runtime

**Solution**:

- inspect `session.failure` plus per-item `failure`
- use `progress`, `queuedAt`, `startedAt`, `completedAt`, and `lastProgressAt` to see where work stopped
- resubmit a narrower bundle after removing failing URLs
- check server logs with the returned `correlation_id`

---

## GitHub Integration Issues

### "GitHub integration required" when submitting a github.com URL

The bot and API have no anonymous GitHub path. Connect first:

```bash
# Via PAT (no Redis or OAuth App required)
curl -X POST http://localhost:8000/v1/auth/github/pat \
  -H "Authorization: Bearer <jwt>" \
  -H "Content-Type: application/json" \
  -d '{"token": "ghp_..."}'

# Or via the web UI: Preferences -> GitHub Integration
```

### 401 from GitHub API / integration status shows `needs_reauth`

The stored token was revoked on GitHub's side. The integration status is set to `needs_reauth` automatically. Logs will contain `event=needs_reauth_dm_skipped` (background workers do not send Telegram DMs directly).

**Fix:** disconnect and reconnect:

```bash
curl -X DELETE http://localhost:8000/v1/auth/github \
  -H "Authorization: Bearer <jwt>"
# Then POST /v1/auth/github/pat with a fresh token
```

### 503 on POST /v1/auth/github/device/start

Either `REDIS_URL` is not configured or `GITHUB_OAUTH_APP_CLIENT_ID` is unset. The Device Flow requires both. The PAT path (`POST /v1/auth/github/pat`) has no such dependency.

```bash
grep GITHUB_OAUTH_APP_CLIENT_ID .env
grep REDIS_URL .env
```

### Daily sync did not import all starred repositories

The `GITHUB_SYNC_LLM_DAILY_BUDGET` cap was reached. Remaining repos are stored with `pending_analysis=true`.

```bash
# Check how many are pending
psql "$DATABASE_URL" -c "SELECT COUNT(*) FROM repositories WHERE pending_analysis = true AND user_id = <id>;"

# Trigger another sync run (resets day counter)
python -m app.cli.sync_github_stars --user-id <id>

# Or increase the daily budget
echo "GITHUB_SYNC_LLM_DAILY_BUDGET=100" >> .env
```

### "No module named 'cryptography'" at startup

The `cryptography` package (Fernet) is not installed.

```bash
pip install -r requirements.txt
# Verify
python -c "from cryptography.fernet import Fernet; print('ok')"
```

### Encryption key changed; existing GitHub tokens are unreadable

Changing `GITHUB_TOKEN_ENCRYPTION_KEY` without rotating breaks all existing integrations. Follow the MultiFernet rotation procedure documented in `app/security/token_crypto.py`:

1. Add the new key as `MultiFernet([new_key, old_key])` -- this decrypts with the old key and re-encrypts with the new one.
2. Run a one-off migration pass to re-encrypt all rows.
3. Switch to `MultiFernet([new_key])` and remove the old key from config.

If the old key is already lost, all affected users must reconnect their GitHub integrations.

---

## MCP Server Issues

### Connection Failures

**Symptom**: Claude Desktop can't connect to MCP server.

**Cause**: MCP server not running or wrong configuration in Claude config.

**Solution**:

1. **Start MCP server**:

   ```bash
   python -m app.cli.mcp_server
   ```

2. **Verify Claude config** (`~/Library/Application Support/Claude/claude_desktop_config.json`):

   ```json
   {
     "mcpServers": {
       "ratatoskr": {
         "command": "python",
         "args": ["-m", "app.cli.mcp_server"],
         "cwd": "/path/to/ratatoskr",
         "env": {
           "PYTHONPATH": "/path/to/ratatoskr"
         }
       }
     }
   }
   ```

3. **Restart Claude Desktop**

### Tool Execution Errors

**Symptom**: "Tool failed to execute" in Claude Desktop.

**Cause**: MCP tool encountered error (database issue, missing env vars, etc.).

**Solution**:

```bash
# Enable MCP debug logging
echo "MCP_LOG_LEVEL=DEBUG" >> .env

# Check MCP server logs
tail -f /var/log/ratatoskr/mcp.log

# If using SSE, ensure user scoping is configured
echo "MCP_TRANSPORT=sse" >> .env
echo "MCP_USER_ID=123456789" >> .env
```

### Hosted SSE Returns 401 or 403

**Symptom**: Hosted MCP connects to `/sse` but every request is rejected.

**Cause**:

- `MCP_AUTH_MODE=jwt` is enabled but the client did not send a bearer token
- the bearer token is expired or invalid
- the server is accidentally still relying on startup scope instead of request auth

**Solution**:

```bash
grep MCP_AUTH_MODE .env
grep MCP_USER_ID .env
```

For hosted mode:

- set `MCP_AUTH_MODE=jwt`
- leave `MCP_USER_ID` unset
- make sure the MCP client sends `Authorization: Bearer <access_token>` on SSE requests

### Trusted Gateway Forwarding Fails

**Symptom**: Hosted MCP works with direct bearer auth but fails behind a reverse proxy or MCP gateway.

**Cause**:

- `MCP_FORWARDING_SECRET` is missing or mismatched
- the gateway forwarded a user ID instead of the original bearer token
- the forwarded header names do not match the configured names

**Solution**:

```bash
grep MCP_FORWARDING_SECRET .env
grep MCP_FORWARDED_ACCESS_TOKEN_HEADER .env
grep MCP_FORWARDED_SECRET_HEADER .env
```

The gateway must forward:

- the original access token in `MCP_FORWARDED_ACCESS_TOKEN_HEADER`
- the shared forwarding secret in `MCP_FORWARDED_SECRET_HEADER`

### Hosted MCP Reads Work but Aggregation Writes Fail

**Symptom**: Search and article tools work, but `create_aggregation_bundle` fails.

**Cause**: The MCP process is using a read-only database mount or a runtime profile intended for read-only tools.

**Solution**:

- use a writable database path for the MCP deployment
- keep the read-only Docker profile for read tools only
- verify the same scoped user can create bundles through `/v1/aggregations`

---

## Performance Issues

### Slow Summarization

**Symptom**: Summaries take >30 seconds to generate.

**Cause**: Slow LLM model, large article, or network latency.

**Solution**:

```bash
# Use faster model
echo "OPENROUTER_MODEL=qwen/qwen3-max" >> .env  # Faster than DeepSeek

# Reduce context window
echo "MAX_CONTENT_LENGTH_TOKENS=30000" >> .env  # Default: 50000

# Enable content chunking
echo "CHUNKING_ENABLED=true" >> .env

# Increase concurrency
echo "MAX_CONCURRENT_CALLS=5" >> .env  # Default: 4

# Restart bot
docker restart ratatoskr
```

### High Memory Usage

**Symptom**: Bot crashes with "Out of memory" or high RAM usage.

**Cause**: Large embedding models or Qdrant index memory usage.

**Solution**:

```bash
# Use smaller embedding model
EMBEDDING_PROVIDER=local  # uses all-MiniLM-L6-v2 by default (~100 MB)

# Disable Qdrant if not needed
echo "QDRANT_REQUIRED=false" >> .env

# Restart bot with memory limit (Docker)
docker run --memory=1g ratatoskr
```

### Token Counting Overhead

**Symptom**: High CPU usage during token counting.

**Cause**: tiktoken encoding/decoding for every request.

**Solution**:

```bash
# Use faster token estimation (less accurate but much faster)
echo "TOKEN_COUNTING_MODE=fast" >> .env  # Uses len(text)//4 approximation

# Or reduce token counting frequency
echo "TOKEN_COUNTING_CACHE_SIZE=1000" >> .env

# Restart bot
docker restart ratatoskr
```

---

## Debugging Strategies

### 1. Start Simple

Before diving deep:

1. **Check bot is running**: `docker ps` or `pgrep -f bot.py`
2. **Check logs**: `docker logs ratatoskr` or `tail -f /var/log/ratatoskr/app.log`
3. **Test basic command**: Send `/start` to bot, verify it responds

### 2. Enable Debug Logging

```bash
# Enable debug logging
echo "LOG_LEVEL=DEBUG" >> .env

# Enable bounded payload previews; tokens, prompts, raw content, and private URLs stay redacted
echo "DEBUG_PAYLOADS=1" >> .env

# Restart bot
docker restart ratatoskr

# Watch logs in real-time
docker logs -f ratatoskr
```

### 3. Use CLI Tools

Test components in isolation:

```bash
# Test URL summarization (bypasses Telegram)
python -m app.cli.summary --url https://example.com/article

# Test search
python -m app.cli.search --query "python tutorial"

# Test database
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT count(*) FROM summaries;"

# Test Qdrant
python -m app.cli.backfill_vector_store --dry-run
```

### 4. Inspect Database

Use correlation IDs to trace requests:

```bash
docker exec -it ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr

-- Find failed requests
SELECT id, input_url AS url, status, last_error FROM requests WHERE status = 'error' LIMIT 10;

-- See Firecrawl responses
SELECT request_id, http_status, firecrawl_success FROM crawl_results WHERE firecrawl_success = false;

-- See LLM failures
SELECT request_id, model, error FROM llm_calls WHERE error IS NOT NULL;

-- See summary validation errors
SELECT request_id, validation_errors FROM summaries WHERE validation_errors IS NOT NULL;
```

### 5. Test External APIs Manually

Isolate whether issue is with bot or external service:

```bash
# Test self-hosted Firecrawl sidecar
curl -X POST http://localhost:3002/v1/scrape \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com"}' | jq .

# Test Crawl4AI sidecar
curl http://localhost:11235/health | jq .

# Test Defuddle sidecar
curl http://localhost:3003/health | jq .

# Test OpenRouter
curl -X POST https://openrouter.ai/api/v1/chat/completions \
  -H "Authorization: Bearer $OPENROUTER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek/deepseek-v4-flash",
    "messages": [{"role": "user", "content": "Hello"}]
  }' | jq .
```

### 6. Compare Working vs Broken

If something used to work:

```bash
# Check git history for changes
git log --oneline --since="2 weeks ago"

# Diff config files
git diff HEAD~10 .env

# Check if environment changed (Python version, dependencies)
pip list | grep -i firecrawl

# Rollback to last working version
git checkout <commit_hash>
docker build -f ops/docker/Dockerfile -t ratatoskr .
docker run ratatoskr
```

### 7. Minimal Reproduction

Strip down to simplest failing case:

1. Test with single, simple URL (not complex SPA or paywalled site)
2. Disable optional features (web search, Qdrant, Redis)
3. Use minimal config (only required env vars)
4. Test with default models (not experimental or unstable models)

### 8. Check System Resources

```bash
# Disk space
df -h

# Memory
free -h

# CPU
top -bn1 | grep "Cpu(s)"

# Network
curl -s http://localhost:3002/health   # self-hosted Firecrawl sidecar
ping -c 3 openrouter.ai
```

---

## Getting Help

If you're still stuck after trying these steps:

1. **Gather diagnostics**: - Correlation ID - Relevant log excerpts - Database query results (requests, llm_calls, crawl_results) - Environment configuration (redact API keys!)

2. **Check existing issues**: [GitHub Issues](https://github.com/po4yka/ratatoskr/issues)

3. **Open new issue** with: - Clear title (e.g., "Firecrawl timeouts on all URLs") - Steps to reproduce - Expected vs actual behavior - Diagnostics from step 1 - Version info (`git rev-parse HEAD`)

4. **Include correlation ID** in issue title/description for faster debugging

---

## Related Documentation

- [environment_variables.md](environment-variables.md) - Full configuration reference
- [DEPLOYMENT.md](../guides/deploy-production.md) - Setup and deployment guides
- [FAQ.md](../explanation/faq.md) - Frequently asked questions
- [SPEC.md](../SPEC.md) - Technical specification

---

**Last Updated**: 2026-04-30
