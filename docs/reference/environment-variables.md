# Environment Variables Reference

Complete reference for all Ratatoskr configuration. Source of truth: `app/config/` (entrypoint `app/config/settings.py`).

**Phase 1 status**: `.env.example` is intentionally minimal. Optional power-user settings should move to `ratatoskr.yaml`; see [Optional YAML Configuration](config-file.md).

**Last Updated**: 2026-04-30

---

## First-Run Required Variables

Only these assignments remain active in `.env.example`:

| Variable | Required when | Owner |
| --- | --- | --- |
| `API_ID` | Always for Telegram bot runtime | `app/config/telegram.py::TelegramConfig` |
| `API_HASH` | Always for Telegram bot runtime | `app/config/telegram.py::TelegramConfig` |
| `BOT_TOKEN` | Always for Telegram bot runtime | `app/config/telegram.py::TelegramConfig` |
| `ALLOWED_USER_IDS` | Always for owner-only access | `app/config/telegram.py::TelegramConfig` |
| `OPENROUTER_API_KEY` | Always for the default OpenRouter quickstart path | `app/config/llm.py::OpenRouterConfig` |

`JWT_SECRET_KEY` is required only when web/API/browser-extension JWT auth is enabled. Firecrawl Cloud is optional; self-hosted Firecrawl and non-Firecrawl scraper providers exist.

## Phase 1 `.env.example` Inventory

This table categorizes every uncommented assignment that existed in `.env.example` before Phase 1 consolidation. The action column describes how the variable is handled after this change.

| Variable | Owner | Category | Phase 1 action |
| --- | --- | --- | --- |
| `API_ID` | `app/config/telegram.py::TelegramConfig` | required | Keep in `.env.example` |
| `API_HASH` | `app/config/telegram.py::TelegramConfig` | required | Keep in `.env.example` |
| `BOT_TOKEN` | `app/config/telegram.py::TelegramConfig` | required | Keep in `.env.example` |
| `ALLOWED_USER_IDS` | `app/config/telegram.py::TelegramConfig` | required | Keep in `.env.example` |
| `ALLOWED_CLIENT_IDS` | `app/config/runtime.py::RuntimeConfig` | optional-defaulted | Move to `ratatoskr.yaml` or rely on code default |
| `JWT_SECRET_KEY` | `app/config/runtime.py::RuntimeConfig` | required only when web/API/browser-extension auth is enabled | Keep commented in `.env.example` |
| `FIRECRAWL_API_KEY` | `app/config/firecrawl.py::FirecrawlConfig` | optional-defaulted | Move to `ratatoskr.yaml` or rely on code default |
| `SCRAPER_*` / `FIRECRAWL_SELF_HOSTED_*` | `app/config/scraper.py::ScraperConfig` | optional-defaulted | Move to `ratatoskr.yaml` or rely on code defaults |
| `OPENROUTER_API_KEY` | `app/config/llm.py::OpenRouterConfig` | required | Keep in `.env.example` |
| `OPENROUTER_MODEL`, `OPENROUTER_FALLBACK_MODELS`, `OPENROUTER_LONG_CONTEXT_MODEL`, `OPENROUTER_FLASH_MODEL`, `OPENROUTER_FLASH_FALLBACK_MODELS`, `OPENROUTER_HTTP_REFERER`, `OPENROUTER_X_TITLE` | `app/config/llm.py::OpenRouterConfig` | optional-defaulted | Move to `ratatoskr.yaml` or rely on code defaults |
| `YOUTUBE_*` | `app/config/media.py::YouTubeConfig` | optional-defaulted | Move to `ratatoskr.yaml` or rely on code defaults |
| `TWITTER_*` | `app/config/twitter.py::TwitterConfig` | optional-defaulted | Move to `ratatoskr.yaml`; keep disabled unless explicitly needed |
| `DATABASE_URL`, `POSTGRES_PASSWORD` | `app/config/database.py::DatabaseConfig` | required | Set in `.env`; do not commit |
| `DB_OPERATION_TIMEOUT`, `DB_MAX_RETRIES`, `DB_JSON_*` | `app/config/runtime.py::RuntimeConfig`, `app/config/database.py::DatabaseConfig` | optional-defaulted | Move to `ratatoskr.yaml` or rely on code defaults |
| `SUMMARY_CONTRACT_BACKEND`, `MIGRATION_SHADOW_MODE_*`, `MIGRATION_INTERFACE_*`, `MIGRATION_TELEGRAM_RUNTIME_TIMEOUT_MS`, `MIGRATION_CUTOVER_EVENTS_FILE`, `MIGRATION_RELEASE_WINDOW_DAYS` | legacy migration runtime controls | deprecated/removable | Remove; Phase 1 startup rejects deprecated shadow-mode env vars |
| `LOG_LEVEL`, `DEBUG_PAYLOADS`, `REQUEST_TIMEOUT_SEC`, `PREFERRED_LANG`, `MAX_CONCURRENT_CALLS`, `SUMMARY_STREAMING_*` | `app/config/runtime.py::RuntimeConfig` | optional-defaulted | Move to `ratatoskr.yaml` or rely on code defaults |
| `TELEGRAM_MAX_*`, `TELEGRAM_MIN_MESSAGE_INTERVAL_MS`, `TELEGRAM_DRAFT_*` | `app/config/telegram.py::TelegramLimitsConfig`, `TelegramConfig` | optional-defaulted | Move to `ratatoskr.yaml` or rely on code defaults |
| `MAX_TEXT_LENGTH_KB` | `app/config/content.py::ContentLimitsConfig` | optional-defaulted | Move to `ratatoskr.yaml` or rely on code default |
| `MCP_*` | `app/config/integrations.py::McpConfig` | optional-defaulted | Move to `ratatoskr.yaml` or rely on code defaults |
| `GRAFANA_ADMIN_PASSWORD` | `ops/docker/docker-compose.monitoring.yml` | optional-defaulted | Keep in monitoring deployment override, not first-run `.env.example` |

The grouped rows above cover all 106 pre-consolidation active assignments: Telegram/access (6), Firecrawl/scraper (30), OpenRouter (8), YouTube (8), Twitter/X (12), database/runtime/migration (34), MCP (5), monitoring (1), and content limits (1).

---

## Quick Configuration Profiles

### Minimal Setup

```bash
API_ID=your_api_id
API_HASH=your_api_hash
BOT_TOKEN=your_bot_token
ALLOWED_USER_IDS=your_user_id
OPENROUTER_API_KEY=your_openrouter_key
```

**Use case**: first Telegram summary through the default OpenRouter path.

### Optional Runtime Tuning

```yaml
runtime:
  log_level: INFO
  request_timeout_sec: 60
scraper:
  profile: balanced
```

**Use case**: production tuning without expanding `.env`.

### Optional Feature Configuration

```yaml
youtube:
  enabled: true
twitter:
  enabled: false
mcp:
  enabled: false
```

**Use case**: optional surfaces and power-user knobs.

---

## [REQUIRED] Core Variables

| Variable | Description |
| ---------- | ------------- |
| `API_ID` | Telegram API ID (from https://my.telegram.org/apps) |
| `API_HASH` | Telegram API hash |
| `BOT_TOKEN` | Telegram bot token (from BotFather) |
| `ALLOWED_USER_IDS` | Comma-separated Telegram user IDs for allowlist-gated bot/API/MCP paths. When empty, JWT API and hosted MCP auth run fail-open, while Telegram bot access and some onboarding paths remain separately constrained. |
| `OPENROUTER_API_KEY` | OpenRouter API key |

## [OPTIONAL] LLM Provider Selection

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `LLM_PROVIDER` | `openrouter` | Active LLM backend: `openrouter`, `openai`, or `anthropic` |
| `OPENAI_API_KEY` | _(empty)_ | OpenAI API key (when using `openai` provider) |
| `OPENAI_MODEL` | `gpt-4o` | OpenAI model name |
| `OPENAI_FALLBACK_MODELS` | `gpt-4o-mini` | Comma-separated fallback models |
| `OPENAI_ORGANIZATION` | _(none)_ | OpenAI organization ID |
| `OPENAI_ENABLE_STRUCTURED_OUTPUTS` | `true` | Enable structured output mode |
| `ANTHROPIC_API_KEY` | _(empty)_ | Anthropic API key (when using `anthropic` provider) |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-5-20250929` | Anthropic model name |
| `ANTHROPIC_FALLBACK_MODELS` | `claude-3-5-haiku-20241022` | Comma-separated fallback models |
| `ANTHROPIC_ENABLE_STRUCTURED_OUTPUTS` | `true` | Enable structured output mode |

## [REQUIRED] OpenRouter (Default LLM Provider)

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `OPENROUTER_MODEL` | `deepseek/deepseek-v4-flash` | Primary model |
| `OPENROUTER_FALLBACK_MODELS` | `qwen/qwen3.6-plus-04-02,moonshotai/kimi-k2-0905,minimax/minimax-m2` | Comma-separated fallback chain |
| `OPENROUTER_LONG_CONTEXT_MODEL` | `google/gemini-3-flash-preview` | Model for long-context content (1M ctx) |
| `OPENROUTER_TEMPERATURE` | `0.2` | Sampling temperature (0-2) |
| `OPENROUTER_TOP_P` | _(none)_ | Top-p sampling |
| `OPENROUTER_MAX_TOKENS` | _(none)_ | Max completion tokens |
| `OPENROUTER_HTTP_REFERER` | _(none)_ | Attribution referer |
| `OPENROUTER_X_TITLE` | _(none)_ | Attribution title |
| `OPENROUTER_PROVIDER_ORDER` | _(none)_ | Comma-separated provider priority |
| `OPENROUTER_ENABLE_STATS` | `false` | Include usage stats in response |
| `OPENROUTER_ENABLE_STRUCTURED_OUTPUTS` | `true` | Enable structured JSON output |
| `OPENROUTER_STRUCTURED_OUTPUT_MODE` | `json_schema` | Mode: `json_schema` or `json_object` |
| `OPENROUTER_REQUIRE_PARAMETERS` | `true` | Require all schema parameters |
| `OPENROUTER_AUTO_FALLBACK_STRUCTURED` | `true` | Auto-fallback from json_schema to json_object |
| `OPENROUTER_MAX_RESPONSE_SIZE_MB` | `10` | Max response payload size (MB) |
| `OPENROUTER_SUMMARY_TEMPERATURE_RELAXED` | _(none)_ | Temperature override for relaxed retry |
| `OPENROUTER_SUMMARY_TOP_P_RELAXED` | _(none)_ | Top-p override for relaxed retry |
| `OPENROUTER_SUMMARY_TEMPERATURE_JSON` | _(none)_ | Temperature override for JSON fallback |
| `OPENROUTER_SUMMARY_TOP_P_JSON` | _(none)_ | Top-p override for JSON fallback |
| `OPENROUTER_ENABLE_PROMPT_CACHING` | `true` | Enable prompt caching for supported providers |
| `OPENROUTER_PROMPT_CACHE_TTL` | `ephemeral` | Cache TTL for non-Anthropic explicit-cache providers (Google): `ephemeral` (5min) or `1h` |
| `OPENROUTER_PROMPT_CACHE_TTL_ANTHROPIC` | `1h` | Cache TTL for Anthropic models specifically. `1h` (2x write / 0.10x read) amortizes positively across batched requests vs `ephemeral` (1.25x write / 0.10x read) |
| `OPENROUTER_CACHE_SYSTEM_PROMPT` | `true` | Cache system message for reuse |
| `OPENROUTER_CACHE_LARGE_CONTENT_THRESHOLD` | `4096` | Min tokens to auto-cache (Gemini requires 4096) |

## Content-Aware Model Routing

Optional routing layer that selects different models by content tier (technical, sociopolitical, default) and content characteristics. Enable with `MODEL_ROUTING_ENABLED=true`. Owner: `app/config/llm.py::ModelRoutingConfig`.

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `MODEL_ROUTING_ENABLED` | `false` | Enable content-aware routing |
| `MODEL_ROUTING_DEFAULT` | `deepseek/deepseek-v4-flash` | Model for general content |
| `MODEL_ROUTING_TECHNICAL` | `deepseek/deepseek-v4-pro` | Model for technical/research content |
| `MODEL_ROUTING_SOCIOPOLITICAL` | `x-ai/grok-4.20-beta` | Model for political/historical/opinion content |
| `MODEL_ROUTING_LONG_CONTEXT` | `qwen/qwen3.6-plus-04-02` | Model for content exceeding token threshold |
| `MODEL_ROUTING_LONG_CONTEXT_THRESHOLD_TOKENS` | `180000` | Token count above which the long-context model is used (~4 chars per token); under Gemini 2.5 Pro 200K pricing cliff |
| `MODEL_ROUTING_VISION` | _(none)_ | Model to use when content has images; opt-in, no default |
| `MODEL_ROUTING_QUICK` | _(none)_ | Model for short-form content (tweets, forwarded posts); opt-in, no default |
| `MODEL_ROUTING_QUICK_THRESHOLD_TOKENS` | `500` | Token count at or below which the quick model is used |
| `MODEL_ROUTING_FALLBACK_MODELS` | `deepseek/deepseek-v4-flash,qwen/qwen3.6-plus-04-02,minimax/minimax-m2` | Shared fallback chain (used when no tier-specific list is set) |
| `MODEL_ROUTING_TECHNICAL_FALLBACK_MODELS` | _(empty)_ | Fallback chain for technical tier (overrides shared when non-empty) |
| `MODEL_ROUTING_SOCIOPOLITICAL_FALLBACK_MODELS` | _(empty)_ | Fallback chain for sociopolitical tier (overrides shared when non-empty) |
| `MODEL_ROUTING_DEFAULT_FALLBACK_MODELS` | _(empty)_ | Fallback chain for default tier (overrides shared when non-empty) |

Override priority: vision > quick > long-context > content-tier.

## [ADVANCED] Firecrawl (Content Extraction)

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `FIRECRAWL_TIMEOUT_SEC` | `90` | Request timeout (10-300s) |
| `FIRECRAWL_WAIT_FOR_MS` | `3000` | JS content load wait (0-30000ms) |
| `FIRECRAWL_MAX_CONNECTIONS` | `10` | Max HTTP connections |
| `FIRECRAWL_MAX_KEEPALIVE_CONNECTIONS` | `5` | Max keepalive connections |
| `FIRECRAWL_KEEPALIVE_EXPIRY` | `30.0` | Keepalive expiry (seconds) |
| `FIRECRAWL_RETRY_MAX_ATTEMPTS` | `3` | Max retry attempts (0-10) |
| `FIRECRAWL_RETRY_INITIAL_DELAY` | `1.0` | Initial retry delay (seconds) |
| `FIRECRAWL_RETRY_MAX_DELAY` | `10.0` | Max retry delay (seconds) |
| `FIRECRAWL_RETRY_BACKOFF_FACTOR` | `2.0` | Backoff multiplier |
| `FIRECRAWL_CREDIT_WARNING_THRESHOLD` | `1000` | Credit warning level |
| `FIRECRAWL_CREDIT_CRITICAL_THRESHOLD` | `100` | Credit critical level |
| `FIRECRAWL_MAX_RESPONSE_SIZE_MB` | `50` | Max response size (MB) |
| `FIRECRAWL_MAX_AGE_SECONDS` | `172800` | Max content age (seconds, default 2 days) |
| `FIRECRAWL_REMOVE_BASE64_IMAGES` | `true` | Strip base64 images |
| `FIRECRAWL_BLOCK_ADS` | `true` | Block ads during scrape |
| `FIRECRAWL_SKIP_TLS_VERIFICATION` | `true` | Skip TLS verification |
| `FIRECRAWL_INCLUDE_MARKDOWN` | `true` | Include markdown format |
| `FIRECRAWL_INCLUDE_HTML` | `true` | Include HTML format |
| `FIRECRAWL_INCLUDE_LINKS` | `false` | Include extracted links |
| `FIRECRAWL_INCLUDE_SUMMARY` | `false` | Include auto-summary |
| `FIRECRAWL_INCLUDE_IMAGES` | `false` | Include image URLs |
| `FIRECRAWL_ENABLE_SCREENSHOT` | `false` | Enable page screenshot |
| `FIRECRAWL_SCREENSHOT_FULL_PAGE` | `true` | Full-page screenshot |
| `FIRECRAWL_SCREENSHOT_QUALITY` | `80` | Screenshot JPEG quality (1-100) |
| `FIRECRAWL_JSON_PROMPT` | _(none)_ | Custom JSON extraction prompt |

## Multi-Provider Scraper Chain

Content extraction uses an ordered chain of providers. Each provider is tried in sequence until one succeeds. Configuration lives in `app/config/scraper.py`.

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `SCRAPER_ENABLED` | `true` | Global master switch for article scraper chain |
| `SCRAPER_PROFILE` | `balanced` | Scraper tuning profile: `fast`, `balanced`, `robust` |
| `SCRAPER_BROWSER_ENABLED` | `true` | Master switch for browser-based providers (`playwright`, `crawlee`) |
| `SCRAPER_FORCE_PROVIDER` | _(none)_ | Force single provider token (`scrapling`, `crawl4ai`, `firecrawl`, `defuddle`, `playwright`, `crawlee`, `direct_html`, `scrapegraph_ai`) |
| `SCRAPER_JS_HEAVY_HOSTS` | _(none)_ | CSV host list for JS-heavy heuristic overlays |
| `SCRAPER_MIN_CONTENT_LENGTH` | `400` | Minimum extracted text length to accept content |
| `SCRAPER_PROVIDER_ORDER` | `["scrapling", "crawl4ai", "firecrawl", "defuddle", "playwright", "crawlee", "direct_html", "scrapegraph_ai"]` | Ordered list of scraping providers to try |
| `SCRAPER_SCRAPLING_ENABLED` | `true` | Enable Scrapling in-process provider |
| `SCRAPER_SCRAPLING_TIMEOUT_SEC` | `30` | Scrapling fetch timeout (seconds) |
| `SCRAPER_SCRAPLING_STEALTH_FALLBACK` | `true` | Try stealth fetch if basic fetch returns thin content |
| `SCRAPER_CRAWL4AI_ENABLED` | `true` | Enable Crawl4AI REST API provider (self-hosted Docker sidecar) |
| `SCRAPER_CRAWL4AI_URL` | `http://crawl4ai:11235` | Crawl4AI service base URL |
| `SCRAPER_CRAWL4AI_TOKEN` | _(empty)_ | Crawl4AI bearer token (optional, for secured instances) |
| `SCRAPER_CRAWL4AI_TIMEOUT_SEC` | `60` | Crawl4AI request timeout (seconds) |
| `SCRAPER_CRAWL4AI_CACHE_MODE` | `BYPASS` | Crawl4AI cache mode: `ENABLED`, `DISABLED`, `BYPASS`, `READ_ONLY`, `WRITE_ONLY` |
| `SCRAPER_DEFUDDLE_ENABLED` | `true` | Enable Defuddle HTTP API provider (self-hosted) |
| `SCRAPER_DEFUDDLE_TIMEOUT_SEC` | `20` | Defuddle request timeout (seconds) |
| `SCRAPER_DEFUDDLE_API_BASE_URL` | `http://defuddle-api:3003` | Defuddle API base URL (self-hosted Docker Compose service). Pointing at `https://defuddle.md` logs a deprecation warning. |
| `FIRECRAWL_SELF_HOSTED_ENABLED` | `false` | Enable self-hosted Firecrawl provider (cloud Firecrawl is no longer supported in the scraper chain) |
| `FIRECRAWL_SELF_HOSTED_URL` | `http://firecrawl-api:3002` | Self-hosted Firecrawl base URL |
| `FIRECRAWL_SELF_HOSTED_API_KEY` | `fc-ratatoskr-local` | Self-hosted Firecrawl API key |
| `SCRAPER_FIRECRAWL_TIMEOUT_SEC` | `90` | Firecrawl timeout for article chain |
| `SCRAPER_FIRECRAWL_WAIT_FOR_MS` | `3000` | Firecrawl wait-for milliseconds for article chain |
| `SCRAPER_FIRECRAWL_MAX_RETRIES` | `3` | Firecrawl retries for article chain |
| `SCRAPER_FIRECRAWL_MAX_CONNECTIONS` | `10` | Firecrawl connection pool size for article chain |
| `SCRAPER_FIRECRAWL_MAX_KEEPALIVE_CONNECTIONS` | `5` | Firecrawl keepalive pool size for article chain |
| `SCRAPER_FIRECRAWL_KEEPALIVE_EXPIRY` | `30.0` | Firecrawl keepalive expiry (seconds) for article chain |
| `SCRAPER_FIRECRAWL_MAX_RESPONSE_SIZE_MB` | `50` | Firecrawl max response size for article chain |
| `SCRAPER_PLAYWRIGHT_ENABLED` | `true` | Enable Playwright rendering fallback provider |
| `SCRAPER_PLAYWRIGHT_HEADLESS` | `true` | Run Playwright browser headless in scraper fallback |
| `SCRAPER_PLAYWRIGHT_TIMEOUT_SEC` | `30` | Playwright render timeout (seconds) |
| `SCRAPER_PLAYWRIGHT_FINGERPRINT_SLIM` | `false` | Use smaller, lower-overhead Browserforge fingerprints for the Playwright provider |
| `SCRAPER_CRAWLEE_ENABLED` | `true` | Enable Crawlee advanced fallback provider |
| `SCRAPER_CRAWLEE_TIMEOUT_SEC` | `45` | Crawlee stage timeout budget (seconds) |
| `SCRAPER_CRAWLEE_HEADLESS` | `true` | Run Crawlee Playwright stage in headless mode |
| `SCRAPER_CRAWLEE_MAX_RETRIES` | `2` | Max retries per Crawlee stage |
| `SCRAPER_DIRECT_HTML_ENABLED` | `true` | Enable direct HTML fallback provider |
| `SCRAPER_DIRECT_HTML_TIMEOUT_SEC` | `30` | Direct HTML fetch timeout (seconds) |
| `SCRAPER_DIRECT_HTML_MAX_RESPONSE_MB` | `10` | Direct HTML max streamed response size (MB) |
| `SCRAPER_SCRAPEGRAPH_ENABLED` | `true` | Enable ScrapeGraph-AI last-resort LLM-driven provider (requires `scrapegraphai` installed and `OPENROUTER_API_KEY`) |
| `SCRAPER_SCRAPEGRAPH_TIMEOUT_SEC` | `90` | ScrapeGraph-AI request timeout (seconds) |

**Notes**:

- Scrapling is a free, in-process scraper that requires no API key. It is tried first by default.
- Crawl4AI is a self-hosted Docker sidecar (`crawl4ai` service on port 11235). When the service is not reachable the provider is skipped automatically.
- Firecrawl now only supports self-hosted mode (`FIRECRAWL_SELF_HOSTED_ENABLED=true`). Cloud Firecrawl (`FIRECRAWL_API_KEY`) is no longer used by the article scraper chain; it remains available for the web-search enrichment subsystem.
- Defuddle is now enabled by default and points at the self-hosted Docker Compose service (`http://defuddle-api:3003`). Pointing it at `https://defuddle.md` logs a `defuddle_provider_cloud_url_deprecated` warning.
- Playwright fallback is useful for JS-heavy pages that fail in HTTP-only extractors.
- Crawlee fallback is a single-page advanced fallback (BeautifulSoup stage, then Playwright stage); it is not broad multi-page site crawling in this pipeline.
- `direct_html` is a lightweight fallback using trafilatura for simple pages.
- ScrapeGraph-AI is the last-resort provider. It uses the OpenRouter API key and model to run an in-process LLM-driven scrape. Requires `pip install scrapegraphai`.
- `SCRAPER_PROFILE` multipliers: `fast=0.75`, `balanced=1.0`, `robust=1.35`; retry tuning uses `fast -> max 1`, `robust -> +1 (cap 5)`.
- **Breaking rename (fail-fast)**: startup now errors if legacy variables are present (`SCRAPLING_ENABLED`, `SCRAPLING_TIMEOUT_SEC`, `SCRAPLING_STEALTH_FALLBACK`, `SCRAPER_DIRECT_HTTP_ENABLED`).

**Breaking rename map**:

| Old | New |
| --- | --- |
| `SCRAPLING_ENABLED` | `SCRAPER_SCRAPLING_ENABLED` |
| `SCRAPLING_TIMEOUT_SEC` | `SCRAPER_SCRAPLING_TIMEOUT_SEC` |
| `SCRAPLING_STEALTH_FALLBACK` | `SCRAPER_SCRAPLING_STEALTH_FALLBACK` |
| `SCRAPER_DIRECT_HTTP_ENABLED` | `SCRAPER_DIRECT_HTML_ENABLED` |

## YouTube Video Download

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `YOUTUBE_DOWNLOAD_ENABLED` | `true` | Enable YouTube video downloading |
| `YOUTUBE_STORAGE_PATH` | `/data/videos` | Video storage directory |
| `YOUTUBE_MAX_VIDEO_SIZE_MB` | `500` | Max per-video size (MB) |
| `YOUTUBE_MAX_STORAGE_GB` | `100` | Max total video storage (GB) |
| `YOUTUBE_PREFERRED_QUALITY` | `1080p` | Video quality: 1080p, 720p, 480p, 360p, 240p |
| `YOUTUBE_SUBTITLE_LANGUAGES` | `en,ru` | Preferred subtitle languages |
| `YOUTUBE_AUTO_CLEANUP_ENABLED` | `true` | Auto-delete old videos |
| `YOUTUBE_CLEANUP_AFTER_DAYS` | `30` | Retention period (days) |

## Twitter/X Content Extraction

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `TWITTER_ENABLED` | `true` | Enable Twitter/X URL detection and extraction |
| `TWITTER_PLAYWRIGHT_ENABLED` | `false` | Enable Playwright-based extraction (requires chromium) |
| `TWITTER_FORCE_TIER` | `auto` | Force tier routing: `auto`, `firecrawl`, `playwright` |
| `TWITTER_SCRAPER_PROFILE` | `inherit` | Profile override for Twitter Playwright timeout tuning (`inherit`, `fast`, `balanced`, `robust`) |
| `TWITTER_MAX_CONCURRENT_BROWSERS` | `2` | Max concurrent Twitter Playwright browser sessions |
| `TWITTER_COOKIES_PATH` | `/data/twitter_cookies.txt` | Path to Netscape-format cookies.txt for authenticated extraction |
| `TWITTER_HEADLESS` | `true` | Run Playwright browser in headless mode |
| `TWITTER_PAGE_TIMEOUT_MS` | `15000` | Page load timeout for Playwright (ms) |
| `TWITTER_PREFER_FIRECRAWL` | `true` | Try Firecrawl first before Playwright fallback |
| `TWITTER_ARTICLE_REDIRECT_RESOLUTION_ENABLED` | `true` | Resolve redirects/canonical hints for X Article links before extraction |
| `TWITTER_ARTICLE_RESOLUTION_TIMEOUT_SEC` | `5` | Timeout for article link resolution requests (seconds) |
| `TWITTER_ARTICLE_LIVE_SMOKE_ENABLED` | `false` | Enable optional live smoke checks for article links (manual script only) |

**Two-tier extraction**: By default, Twitter URLs are extracted via Firecrawl (free, no auth needed). If Firecrawl fails (login wall), enable `TWITTER_PLAYWRIGHT_ENABLED` and provide a `cookies.txt` for authenticated extraction.

**Manual live smoke (non-CI)**: Validate real article links with redirect-aware routing and stage-level diagnostics:

```bash
uv run python tools/scripts/twitter_article_live_smoke.py \
  --url "https://t.co/..." \
  --url "https://x.com/i/article/1234567890"
```

## Web Search Enrichment

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `WEB_SEARCH_ENABLED` | `false` | Enable LLM-driven web search (opt-in) |
| `WEB_SEARCH_MAX_QUERIES` | `3` | Max search queries per article (1-10) |
| `WEB_SEARCH_MIN_CONTENT_LENGTH` | `500` | Min content chars to trigger search |
| `WEB_SEARCH_TIMEOUT_SEC` | `10.0` | Search operation timeout (1-60s) |
| `WEB_SEARCH_MAX_CONTEXT_CHARS` | `2000` | Max injected context chars (500-10000) |
| `WEB_SEARCH_CACHE_TTL_SEC` | `3600` | Search result cache TTL (60-86400s) |

## Deployment

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `APP_ENV` | `development` | Deployment environment: `development`, `staging`, or `production`. Setting `production` enables strict safety checks — see below. |
| `API_PUBLIC_EXPOSURE` | `false` | Set `true` when the API is reachable from the public internet. Triggers the same safety checks as `APP_ENV=production` regardless of `APP_ENV`. |
| `RATE_LIMIT_REDIS_OVERRIDE` | `false` | Emergency override: allow in-memory rate limiting even in production. Must be explicitly set to acknowledge that per-process limits are not shared across workers or restarts. |
| `AUTH_ALLOW_ANY_CLIENT_ID` | `false` | Emergency/development override: allow every syntactically valid `client_id` when `ALLOWED_CLIENT_IDS` is empty. Required if a production/public deployment intentionally runs without a client allowlist. |

### Production Redis requirement

When `APP_ENV=production` or `API_PUBLIC_EXPOSURE=true`, the application **refuses to start** unless both `REDIS_ENABLED=true` and `REDIS_REQUIRED=true` are set. This prevents silent fallback to process-local rate limiting, which is ineffective under multiple workers or after restarts.

To override (e.g. single-process deploy, edge cache handles rate limiting externally), set `RATE_LIMIT_REDIS_OVERRIDE=true` and acknowledge the risk in your deployment notes.

### Production client allowlist requirement

When `APP_ENV=production` or `API_PUBLIC_EXPOSURE=true`, the application **refuses to start** with an empty `ALLOWED_CLIENT_IDS` list unless `AUTH_ALLOW_ANY_CLIENT_ID=true` is set. Local/development mode still allows an empty client allowlist, but startup logs a warning because every syntactically valid `client_id` can authenticate.

## Redis Caching

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `REDIS_ENABLED` | `true` | Enable Redis integration |
| `REDIS_CACHE_ENABLED` | `true` | Enable caching via Redis |
| `REDIS_REQUIRED` | `false` | Fail requests when Redis unavailable. **Must be `true` in production** (enforced automatically when `APP_ENV=production`). |
| `REDIS_URL` | _(none)_ | Full Redis URL (overrides host/port/db) |
| `REDIS_HOST` | `127.0.0.1` | Redis host |
| `REDIS_PORT` | `6379` | Redis port |
| `REDIS_DB` | `0` | Redis database number |
| `REDIS_PASSWORD` | _(none)_ | Redis password |
| `REDIS_PREFIX` | `ratatoskr` | Key prefix for namespacing |
| `REDIS_SOCKET_TIMEOUT` | `5.0` | Socket timeout (seconds) |
| `REDIS_CACHE_TIMEOUT_SEC` | `0.3` | Cache operation timeout (seconds) |
| `REDIS_FIRECRAWL_TTL_SECONDS` | `21600` | Firecrawl response cache TTL (6h) |
| `REDIS_LLM_TTL_SECONDS` | `7200` | LLM response cache TTL (2h) |

## Vector Search / Qdrant

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `QDRANT_URL` | `http://localhost:6333` | Qdrant HTTP endpoint |
| `QDRANT_API_KEY` | _(none)_ | API key for secured Qdrant instances |
| `QDRANT_ENV` | `dev` | Environment label for collection namespacing |
| `QDRANT_USER_SCOPE` | `public` | Tenant scope for collections |
| `QDRANT_COLLECTION_VERSION` | `v1` | Collection version suffix |
| `QDRANT_REQUIRED` | `false` | Fail startup if Qdrant unavailable |
| `QDRANT_CONNECTION_TIMEOUT` | `10.0` | Connection timeout (seconds) |

## Embedding Provider

Controls which embedding backend generates vectors for semantic search.

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `EMBEDDING_PROVIDER` | `local` | `local` (sentence-transformers) or `gemini` (Google Gemini API) |
| `GEMINI_API_KEY` | _(empty)_ | Google Gemini API key (required when `EMBEDDING_PROVIDER=gemini`) |
| `GEMINI_EMBEDDING_MODEL` | `gemini-embedding-2-preview` | Gemini embedding model ID |
| `GEMINI_EMBEDDING_DIMENSIONS` | `768` | Output embedding dimensions (128-3072; Google recommends 768, 1536, or 3072) |
| `EMBEDDING_MAX_TOKEN_LENGTH` | `512` | Max tokens per text chunk for embedding (64-8192; Gemini supports up to 8192) |

**Notes:**

- Switching providers or Gemini output dimensions changes the embedding space. Re-embed all data after switching: `python -m app.cli.backfill_embeddings --force` then `python -m app.cli.backfill_vector_store --force`.
- Qdrant collections are automatically namespaced by Gemini model + dimensionality to avoid mixing incompatible embedding spaces such as `gemini-embedding-001` and `gemini-embedding-2-preview`.
- `google-genai` package is an optional dependency (`pip install ratatoskr[gemini]`). The app works without it when `EMBEDDING_PROVIDER=local`.
- Gemini uses task-type-aware embeddings: `RETRIEVAL_DOCUMENT` for indexing, `RETRIEVAL_QUERY` for search queries.

## Vector-Index Sync (CocoIndex + Reconciler)

See [`docs/cocoindex.md`](../cocoindex.md) for architecture, summary/repository indexing semantics, drift detection, and rollback procedure.

### CocoIndex live updater (opt-in)

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `RATATOSKR_COCOINDEX_ENABLED` | `0` | Enable CocoIndex `FlowLiveUpdater` inside FastAPI |
| `RATATOSKR_COCOINDEX_DSN` | _(DATABASE_URL)_ | Override Postgres DSN (asyncpg prefix stripped automatically) |
| `RATATOSKR_COCOINDEX_POLL_INTERVAL_SEC` | `30` | Seconds between watermark polls when LISTEN/NOTIFY is idle |
| `RATATOSKR_COCOINDEX_LISTEN_CHANNEL` | `ratatoskr_summaries_changed` | Postgres LISTEN/NOTIFY channel |
| `RATATOSKR_COCOINDEX_BATCH_SIZE` | `32` | Rows per processing batch |
| `RATATOSKR_COCOINDEX_POOL_MAX` | `4` | Max psycopg3 connections (counts against `max_connections`) |

### Vector reconciler (Taskiq, on by default)

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `VECTOR_RECONCILE_ENABLED` | `true` | Enable the `ratatoskr.vector.reconcile` Taskiq job |
| `VECTOR_RECONCILE_CRON` | `*/30 * * * *` | Cron expression in UTC |
| `VECTOR_RECONCILE_BATCH_SIZE` | `100` | Maximum stale summaries re-embedded per run |

## [OPTIONAL] ElevenLabs Text-to-Speech (TTS)

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `ELEVENLABS_ENABLED` | `false` | Enable ElevenLabs TTS integration |
| `ELEVENLABS_API_KEY` | _(empty)_ | ElevenLabs API key (required when enabled) |
| `ELEVENLABS_VOICE_ID` | `21m00Tcm4TlvDq8ikWAM` | Voice ID (default: Rachel) |
| `ELEVENLABS_MODEL` | `eleven_multilingual_v2` | TTS model ID |
| `ELEVENLABS_OUTPUT_FORMAT` | `mp3_44100_128` | Audio output format |
| `ELEVENLABS_STABILITY` | `0.5` | Voice stability (0.0-1.0) |
| `ELEVENLABS_SIMILARITY_BOOST` | `0.75` | Voice similarity boost (0.0-1.0) |
| `ELEVENLABS_SPEED` | `1.0` | Speech speed (0.5-2.0) |
| `ELEVENLABS_TIMEOUT_SEC` | `60` | API request timeout (seconds) |
| `ELEVENLABS_MAX_CHARS` | `5000` | Character limit per API request (chunking threshold) |
| `ELEVENLABS_AUDIO_PATH` | `/data/audio` | Directory for cached audio files |

## MCP Server

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `MCP_ENABLED` | `false` | Enable MCP server for AI agent access |
| `MCP_TRANSPORT` | `stdio` | Transport: `stdio` or `sse` |
| `MCP_HOST` | `127.0.0.1` | SSE bind address |
| `MCP_PORT` | `8200` | SSE port |
| `MCP_USER_ID` | _(none)_ | Scope MCP reads to a single user ID |
| `MCP_ALLOW_REMOTE_SSE` | `false` | Allow non-loopback SSE bind host; also disables DNS rebinding protection |
| `MCP_ALLOW_UNSCOPED_SSE` | `false` | Allow SSE without explicit user scope |
| `MCP_AUTH_MODE` | `disabled` | Hosted MCP auth mode: `disabled` or `jwt` |
| `MCP_FORWARDED_ACCESS_TOKEN_HEADER` | `X-Ratatoskr-Forwarded-Access-Token` | Trusted-gateway header for the forwarded original bearer token |
| `MCP_FORWARDED_SECRET_HEADER` | `X-Ratatoskr-MCP-Forwarding-Secret` | Trusted-gateway header for the shared forwarding secret |
| `MCP_FORWARDING_SECRET` | _(none)_ | Shared secret required before trusting forwarded access-token headers |

## Mobile API and Auth

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `JWT_SECRET_KEY` | _(required if API used)_ | JWT signing secret (min 32 chars) |
| `ALLOWED_CLIENT_IDS` | _(empty = allow all only in development or with `AUTH_ALLOW_ANY_CLIENT_ID=true`)_ | Comma-separated allowed client app IDs |
| `API_RATE_LIMIT_WINDOW_SECONDS` | `60` | Rate limit window |
| `API_RATE_LIMIT_COOLDOWN_MULTIPLIER` | `2.0` | Cooldown multiplier on limit exceeded |
| `API_RATE_LIMIT_MAX_CONCURRENT_PER_USER` | `3` | Max concurrent requests per user |
| `API_RATE_LIMIT_DEFAULT` | `100` | Default rate limit |
| `API_RATE_LIMIT_SUMMARIES` | `200` | Summaries endpoint limit |
| `API_RATE_LIMIT_REQUESTS` | `10` | Requests endpoint limit |
| `API_RATE_LIMIT_SEARCH` | `50` | Search endpoint limit |
| `API_RATE_LIMIT_SECRET_LOGIN` | `10` | Dedicated `POST /v1/auth/secret-login` limit |
| `API_RATE_LIMIT_CREDENTIALS_LOGIN` | `10` | Dedicated `POST /v1/auth/credentials-login` limit (separate counter from secret-login so brute-forcing one channel cannot lock out the other) |
| `API_RATE_LIMIT_AGGREGATION_CREATE_USER` | `5` | Aggregation create limit per authenticated user |
| `API_RATE_LIMIT_AGGREGATION_CREATE_CLIENT` | `20` | Aggregation create limit per client ID across users |
| `SYNC_EXPIRY_HOURS` | `1` | Sync session expiry |
| `SYNC_DEFAULT_LIMIT` | `200` | Default sync page size |
| `SYNC_MIN_LIMIT` | `1` | Min sync page size |
| `SYNC_MAX_LIMIT` | `500` | Max sync page size |
| `SYNC_TARGET_PAYLOAD_KB` | `512` | Target sync payload size (KB) |
| `SECRET_LOGIN_ENABLED` | `false` | Enable secret-key login flow |
| `SECRET_LOGIN_MIN_LENGTH` | `32` | Min secret length |
| `SECRET_LOGIN_MAX_LENGTH` | `128` | Max secret length |
| `SECRET_LOGIN_MAX_FAILED_ATTEMPTS` | `5` | Max failed login attempts before lockout |
| `SECRET_LOGIN_LOCKOUT_MINUTES` | `15` | Lockout duration |
| `SECRET_LOGIN_PEPPER` | _(none)_ | **Required when `SECRET_LOGIN_ENABLED=true`.** Pepper used to hash `ClientSecret.secret_hash`. ≥32 chars; MUST be independent of `JWT_SECRET_KEY` and `CREDENTIALS_LOGIN_PEPPER`. Generate with `openssl rand -hex 32`. The previous fallback to `JWT_SECRET_KEY` was removed: rotating the JWT signing key would otherwise invalidate every stored secret hash and lock all machine clients out of secret-login. **Migration**: deployments that previously relied on the fallback must set `SECRET_LOGIN_PEPPER` to the same value as `JWT_SECRET_KEY` once (preserving existing hashes), then rotate to a new independent pepper on the next forced re-issue (`/v1/auth/secret-keys` rotate flow). A short (<32 char) value is rejected at config load. |
| `CREDENTIALS_LOGIN_PEPPER` | _(none)_ | Pepper presence is the gate for credentials login (`POST /v1/auth/credentials-login`) — there is no separate enable flag. ≥32 chars; MUST be independent of `JWT_SECRET_KEY` and `SECRET_LOGIN_PEPPER`. Applied as HMAC-SHA256 pre-hash before argon2id. Generate with `openssl rand -hex 32`. Unset → the route returns `503 Configuration error`; the rest of the API still boots. A short (<32 char) value is rejected at config load. |
| `CREDENTIALS_LOGIN_MAX_FAILED_ATTEMPTS` | `5` | Max failed credential attempts before lockout |
| `CREDENTIALS_LOGIN_LOCKOUT_MINUTES` | `15` | Lockout duration after repeated credential failures |
| `CREDENTIALS_LOGIN_PASSWORD_MIN_LENGTH` | `12` | Minimum password length |
| `CREDENTIALS_LOGIN_PASSWORD_MAX_LENGTH` | `256` | Maximum password length (DoS guard for argon2) |
| `CREDENTIALS_LOGIN_REMEMBER_ME_DAYS` | `30` | Refresh-token TTL when Remember Me is checked (days) |
| `CREDENTIALS_LOGIN_NO_REMEMBER_HOURS` | `12` | Refresh-token TTL when Remember Me is unchecked (hours). Web client also writes tokens to `sessionStorage` in this mode so they vanish on browser close. |
| `CREDENTIALS_LOGIN_ARGON2_TIME_COST` | `3` | argon2id iterations |
| `CREDENTIALS_LOGIN_ARGON2_MEMORY_KIB` | `65536` | argon2id memory cost in KiB (default 64 MiB) |
| `CREDENTIALS_LOGIN_ARGON2_PARALLELISM` | `1` | argon2id parallelism (lanes) |
| `IMPORT_MAX_UPLOAD_BYTES` | `10485760` | Max import upload size in bytes (default 10 MB) |
| `IMPORT_MAX_ITEMS` | `10000` | Max parsed bookmarks per import (default 10 000) |
| `BACKUP_RESTORE_MAX_UPLOAD_BYTES` | `104857600` | Max backup restore upload size in bytes (default 100 MB) |

**External client ID guidance**:

- Use stable, exact client IDs and list them explicitly in `ALLOWED_CLIENT_IDS` for public deployments.
- Do not leave `ALLOWED_CLIENT_IDS` empty in production unless `AUTH_ALLOW_ANY_CLIENT_ID=true` is an intentional, documented deployment decision.
- Recommended prefixes are `cli-...`, `mcp-...`, `automation-...`, `web-...`, and `mobile-...`.
- Non-owner self-service secret creation, rotation, revoke, and listing are intentionally limited to `cli`, `mcp`, and `automation` client types.
- Mobile and web client secrets should remain owner-issued unless the provisioning model is expanded later.

## Channel Digest

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `DIGEST_ENABLED` | `false` | Enable channel digest subsystem |
| `DIGEST_SESSION_NAME` | `digest_userbot` | Telethon session name for the userbot |
| `DIGEST_TIME` | `09:00` | Daily digest delivery time (HH:MM) |
| `DIGEST_TIMEZONE` | `UTC` | Timezone for digest scheduling |
| `DIGEST_MAX_POSTS` | `50` | Max posts per channel to include in digest |
| `DIGEST_MAX_CHANNELS` | `20` | Max channels per user |
| `DIGEST_CONCURRENCY` | `3` | Concurrent channel fetch tasks |
| `DIGEST_MIN_POST_LENGTH` | `100` | Min post character length to include |
| `DIGEST_HOURS_LOOKBACK` | `24` | Hours to look back for new posts |
| `API_BASE_URL` | `http://localhost:8000` | Base URL for the Mobile API (used by digest for session init) |

## Database and Backups

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `DATABASE_URL` | _(required)_ | PostgreSQL DSN, e.g. `postgresql+asyncpg://ratatoskr_app:${POSTGRES_PASSWORD}@postgres:5432/ratatoskr` |
| `POSTGRES_PASSWORD` | _(required)_ | Password for the `ratatoskr_app` role; injected into the compose `postgres` service and used to assemble `DATABASE_URL` |
| `DB_BACKUP_ENABLED` | `1` | Enable scheduled `pg_dump` backups (0/1) |
| `DB_BACKUP_INTERVAL_MINUTES` | `360` | Backup interval |
| `DB_BACKUP_RETENTION` | `14` | Backup retention (days) |
| `DB_BACKUP_DIR` | `/data/backups` | Backup directory inside the bot container |
| `DB_OPERATION_TIMEOUT` | `30.0` | Per-operation timeout (seconds) |
| `DB_MAX_RETRIES` | `3` | Retries on transient `serialization_failure` / deadlock |
| `DB_JSON_MAX_SIZE` | `10000000` | Max JSONB payload size validated at the application layer (bytes, 10MB) |
| `DB_JSON_MAX_DEPTH` | `20` | Max JSON nesting depth validated at the application layer |
| `DB_JSON_MAX_ARRAY_LENGTH` | `10000` | Max JSON array length validated at the application layer |
| `DB_JSON_MAX_DICT_KEYS` | `1000` | Max JSON dictionary keys validated at the application layer |

## Telegram Limits

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `TELEGRAM_MAX_MESSAGE_CHARS` | `3500` | Max chars per reply (safety margin below 4096) |
| `TELEGRAM_MAX_URL_LENGTH` | `2048` | Max URL length (RFC 2616) |
| `TELEGRAM_MAX_BATCH_URLS` | `200` | Max URLs in a batch operation |
| `TELEGRAM_MIN_MESSAGE_INTERVAL_MS` | `100` | Min interval between messages (rate limiting) |
| `TELEGRAM_DRAFT_STREAMING_ENABLED` | `true` | Enable draft updates via `sendMessageDraft` transport |
| `TELEGRAM_DRAFT_MIN_INTERVAL_MS` | `700` | Minimum interval between draft sends (ms) |
| `TELEGRAM_DRAFT_MIN_DELTA_CHARS` | `40` | Minimum meaningful text delta before draft update |
| `TELEGRAM_DRAFT_MAX_CHARS` | `3500` | Maximum characters per draft update payload |

## Content Processing

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `MAX_TEXT_LENGTH_KB` | `50` | Max text length for URL extraction (KB, regex DoS prevention) |
| `URL_FLOW_STREAMING_ENABLED` | `true` | Publish phase + section events to the StreamHub during URL summarization. Drives the Telegram URL-flow draft-message updates and the web SubmitPage's SSE consumer. Set to `false` to use the legacy single-shot reply path. |

## Circuit Breaker

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `CIRCUIT_BREAKER_ENABLED` | `true` | Enable circuit breaker for external services |
| `CIRCUIT_BREAKER_FAILURE_THRESHOLD` | `5` | Failures before opening circuit |
| `CIRCUIT_BREAKER_TIMEOUT_SECONDS` | `60.0` | Wait before half-open state |
| `CIRCUIT_BREAKER_SUCCESS_THRESHOLD` | `2` | Successes needed to close from half-open |

## Background Processor

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `BACKGROUND_REDIS_LOCK_ENABLED` | `true` | Use Redis distributed locks |
| `BACKGROUND_REDIS_LOCK_REQUIRED` | `false` | Fail if Redis unavailable for locking |
| `BACKGROUND_LOCK_TTL_MS` | `300000` | Lock TTL (ms, default 5min) |
| `BACKGROUND_LOCK_SKIP_ON_HELD` | `true` | Skip task if lock already held |
| `BACKGROUND_RETRY_ATTEMPTS` | `3` | Retry attempts for failed tasks |
| `BACKGROUND_RETRY_BASE_DELAY_MS` | `500` | Base retry delay (ms) |
| `BACKGROUND_RETRY_MAX_DELAY_MS` | `5000` | Max retry delay (ms) |
| `BACKGROUND_RETRY_JITTER_RATIO` | `0.2` | Jitter ratio (0-1) |

## Data Retention

Configures scheduled nulling of raw artifact columns (scraped HTML, LLM payloads, Telegram message JSON, video transcripts). The summary, cost, and status columns are never purged. A TTL of `0` disables purge for that subsystem.

| Variable | Type | Default | Description |
|---|---|---|---|
| `RETENTION_ENABLED` | bool | `true` | Master switch. Set to `false` to disable all purge runs. |
| `RETENTION_CRON` | str | `"0 3 * * *"` | UTC cron for the daily purge job (3 am UTC). |
| `RETENTION_BATCH_SIZE` | int | `500` | Max rows updated per subsystem per run. Next run continues the backlog. |
| `RETENTION_TELEGRAM_RAW_DAYS` | int | `30` | Days to keep `telegram_messages` raw columns (`text_full`, `entities_json`, `telegram_raw_json`). `0` = never purge. |
| `RETENTION_CRAWL_CONTENT_DAYS` | int | `7` | Days to keep `crawl_results` content columns (`content_markdown`, `content_html`, `raw_response_json`, `firecrawl_details_json`, `structured_json`, `metadata_json`, `links_json`). `0` = never purge. |
| `RETENTION_LLM_PAYLOAD_DAYS` | int | `90` | Days to keep `llm_calls` request/response columns. Cost, token, and latency fields are always preserved. `0` = never purge. |
| `RETENTION_VIDEO_TRANSCRIPT_DAYS` | int | `30` | Days to keep `video_downloads.transcript_text`. `0` = never purge. |
| `RETENTION_INTERACTION_TEXT_DAYS` | int | `30` | Days to keep `user_interactions.input_text`. `0` = never purge. |
| `RETENTION_REQUEST_CONTENT_DAYS` | int | `30` | Days to keep `requests.content_text` and `requests.error_context_json`. `0` = never purge. |

## Mobile API Server

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `API_HOST` | `0.0.0.0` | API bind address |
| `API_PORT` | `8000` | API listen port |

## Runtime and Debug

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `LOG_LEVEL` | `INFO` | Logging level: DEBUG, INFO, WARNING, ERROR |
| `LOG_TRUNCATE_LENGTH` | `1000` | Max chars for truncated log fields |
| `REQUEST_TIMEOUT_SEC` | `60` | General request timeout |
| `PREFERRED_LANG` | `auto` | Language preference: `auto`, `en`, `ru` |
| `DEBUG_PAYLOADS` | `0` | Enable bounded debug payload previews. Keep disabled in production; tokens, prompts, raw content, and private URLs are redacted by default. |
| `LOG_PRIVACY_REDACT_URLS` | `1` | Redact URL path/query/fragment fields in logs and traces by default; set to `0` only for controlled local debugging. |
| `MAX_CONCURRENT_CALLS` | `4` | Max concurrent Firecrawl/OpenRouter calls |
| `TEXTACY_ENABLED` | `false` | Enable the optional text-normalization pass (historical env var name) |
| `CHUNKING_ENABLED` | `true` | Enable content chunking for long articles |
| `CHUNK_MAX_CHARS` | `200000` | Max chars per content chunk |
| `SUMMARY_PROMPT_VERSION` | `v1` | Summary prompt template version |
| `SUMMARY_STREAMING_ENABLED` | `true` | Enable section-based summary streaming |
| `SUMMARY_STREAMING_MODE` | `section` | Streaming mode (`section` or `disabled`) |
| `SUMMARY_STREAMING_PROVIDER_SCOPE` | `openrouter` | Provider scope for token streaming (`openrouter`, `all`, `disabled`) |
| `TELEGRAM_REPLY_TIMEOUT_SEC` | `30.0` | Timeout for Telegram reply operations |

## LLM Call Timeouts

These knobs govern how long the OpenRouter chat engine spends per model and per call.

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `LLM_CALL_TIMEOUT_SEC` | `300.0` | Total wall-clock budget for one LLM call (across the full fallback ladder) |
| `LLM_PER_MODEL_TIMEOUT_MIN_SEC` | `120.0` | Minimum per-model budget. Per-model timeout is `max(this, LLM_CALL_TIMEOUT_SEC / num_models)` so slow models in long ladders are not starved |
| `LLM_PER_MODEL_TIMEOUT_OVERRIDES` | _(empty)_ | Comma-separated `model=seconds` overrides, e.g. `moonshotai/kimi-k2.5=180,minimax/minimax-m1=240`. Overrides win over the formula above. Malformed entries are skipped with a warning |
| `LLM_CALL_MAX_RETRIES` | `2` | Retries on transient HTTP failures inside a single model attempt |

## Mixed-Source Aggregation Rollout

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `AGGREGATION_BUNDLE_ENABLED` | `true` | Master switch for bundle aggregation in Telegram and API |
| `AGGREGATION_ROLLOUT_STAGE` | `enabled` | Availability stage: `disabled`, `internal`, `owner_beta`, `enabled` |
| `AGGREGATION_META_EXTRACTORS_ENABLED` | `true` | Enable dedicated Threads/Instagram extraction instead of generic article fallback |
| `AGGREGATION_ARTICLE_MEDIA_ENABLED` | `true` | Attach curated article/X image assets to aggregation documents and multimodal summary requests |
| `AGGREGATION_NON_YOUTUBE_VIDEO_ENABLED` | `true` | Enable shared Telegram/Meta video normalization with transcript/audio/OCR fallbacks |

## GitHub Integration

Per-user GitHub credential storage, OAuth Device Flow, and daily stars sync. Configuration owner: `app/config/github.py::GitHubConfig`. Generate the Fernet key with: `python tools/scripts/generate_github_encryption_key.py`.

| Variable | Default | Required | Description | Used by |
|----------|---------|----------|-------------|---------|
| `GITHUB_REQUEST_TIMEOUT_SEC` | `30.0` | No | HTTP timeout (seconds) for all GitHub REST API calls | `app/adapters/github/github_api_client.py` |
| `GITHUB_README_MAX_BYTES` | `51200` | No | Maximum README content size (bytes) to fetch and store; text is truncated at a character boundary | `app/adapters/github/platform_extractor.py` |
| `GITHUB_CONCURRENCY_PER_USER` | `2` | No | Maximum concurrent GitHub API requests per user during sync | `app/tasks/github_sync.py` |
| `GITHUB_OAUTH_APP_CLIENT_ID` | _(none)_ | No — OAuth Device Flow only | GitHub OAuth App client ID; PAT path works without this | `app/api/routers/auth/github.py` |
| `GITHUB_OAUTH_APP_CLIENT_SECRET` | _(none)_ | No — OAuth Device Flow only | GitHub OAuth App client secret; stored as `SecretStr`, never logged | `app/api/routers/auth/github.py` |
| `GITHUB_TOKEN_ENCRYPTION_KEY` | _(none)_ | Yes — when any token is stored | 32-byte URL-safe base64 Fernet key for at-rest token encryption. Missing key raises `MissingEncryptionKeyError` at first use. | `app/security/token_crypto.py` |
| `GITHUB_TOKEN_PREVIOUS_KEYS` | _(none)_ | No | Comma-separated previous Fernet keys kept during a rotation window. Each key must be the same format as `GITHUB_TOKEN_ENCRYPTION_KEY`. Decryption tries all keys; encryption always uses the primary. Remove old keys after running `python -m app.cli.rotate_github_tokens`. | `app/security/token_crypto.py` |
| `GITHUB_SYNC_ENABLED` | `true` | No | Master switch for the Taskiq daily stars sync job; when `false`, the job is not registered with the scheduler | `app/tasks/scheduler.py` |
| `GITHUB_SYNC_CRON` | `0 2 * * *` | No | UTC cron expression for the sync job (default: 02:00 UTC daily) | `app/tasks/scheduler.py` |
| `GITHUB_SYNC_LLM_CONCURRENCY` | `2` | No | Maximum concurrent LLM analysis calls within a single sync run | `app/tasks/github_sync.py` |
| `GITHUB_SYNC_LLM_DAILY_BUDGET` | `100` | No | Maximum LLM calls per calendar day; repos exceeding the cap get `pending_analysis=true` and are re-queued the next day | `app/tasks/github_sync.py` |

**Notes:**

- `GITHUB_TOKEN_ENCRYPTION_KEY` is the only hard requirement when the GitHub integration is used. Without it, `encrypt_token` and `decrypt_token` raise at call time, not at startup, so the rest of the API boots normally.
- `GITHUB_TOKEN_PREVIOUS_KEYS` is optional and used only during key rotation. Set it to the old key value(s) while both keys are live, then remove it after running `python -m app.cli.rotate_github_tokens` to backfill all rows. See `tools/scripts/generate_github_encryption_key.py` for the full rotation procedure.
- OAuth Device Flow additionally requires `GITHUB_OAUTH_APP_CLIENT_ID`, `GITHUB_OAUTH_APP_CLIENT_SECRET`, and a running Redis instance (`REDIS_URL`). `POST /v1/auth/github/device/start` returns 503 when Redis is unavailable.
- `GITHUB_SYNC_ENABLED=false` disables only the scheduled Taskiq job. Manual ingestion via `POST /v1/repositories` and `python -m app.cli.repository` still work.
- The `GITHUB_SYNC_LLM_DAILY_BUDGET` counter resets at the start of each sync run (not at midnight UTC). For owner-only deployments (N=1 user) the effective daily budget equals this value.

---

## Configuration Validation Checklist

Use this checklist to verify your configuration before deploying:

### ✅ Essential Configuration

- [ ] **Telegram API credentials set**: `API_ID`, `API_HASH`, `BOT_TOKEN`
- [ ] **Telegram user allowlist configured**: `ALLOWED_USER_IDS` contains your Telegram user ID if you use the bot or an allowlist-gated rollout stage
- [ ] **Firecrawl API key valid** (if using cloud Firecrawl): Test with `curl -H "Authorization: Bearer $FIRECRAWL_API_KEY" https://api.firecrawl.dev/v1/account`
- [ ] **OpenRouter API key valid**: Test with `curl -H "Authorization: Bearer $OPENROUTER_API_KEY" https://openrouter.ai/api/v1/models`
- [ ] **OpenRouter model specified**: `OPENROUTER_MODEL` set to valid model (e.g., `deepseek/deepseek-v4-flash`)

### ✅ Optional Features (If Enabled)

- [ ] **YouTube**: `YOUTUBE_DOWNLOAD_ENABLED=true` → ffmpeg installed
- [ ] **Web Search**: `WEB_SEARCH_ENABLED=true` → Firecrawl search API accessible
- [ ] **Redis**: `REDIS_ENABLED=true` → Redis server running at `REDIS_URL`
- [ ] **Qdrant**: `QDRANT_URL` points to a running Qdrant server
- [ ] **Mobile API**: `JWT_SECRET_KEY` set → Strong secret (32+ characters)
- [ ] **MCP Server**: `MCP_ENABLED=true` → Claude Desktop config updated
- [ ] **Channel Digest**: `DIGEST_ENABLED=true` → `API_BASE_URL` set, `/init_session` completed
- [ ] **Aggregation rollout**: `AGGREGATION_BUNDLE_ENABLED=true` and `AGGREGATION_ROLLOUT_STAGE` set to the intended exposure stage
- [ ] **Aggregation media/video flags**: `AGGREGATION_META_EXTRACTORS_ENABLED`, `AGGREGATION_ARTICLE_MEDIA_ENABLED`, and `AGGREGATION_NON_YOUTUBE_VIDEO_ENABLED` match the desired rollout scope

### ✅ Performance & Storage

- [ ] **Postgres reachable**: `docker exec ratatoskr-postgres pg_isready -U ratatoskr_app -d ratatoskr` returns ok; `DATABASE_URL` matches the running role/db
- [ ] **YouTube storage configured**: `YOUTUBE_STORAGE_PATH` has sufficient space
- [ ] **Concurrency tuned**: `MAX_CONCURRENT_CALLS` appropriate for your rate limits
- [ ] **Log level set**: `LOG_LEVEL=INFO` for production (DEBUG for troubleshooting)

### ✅ Security

- [ ] **API keys not in git**: `.env` file in `.gitignore`
- [ ] **Access control model chosen**: either populate `ALLOWED_USER_IDS` for allowlist-based rollout, or intentionally leave it empty for multi-user JWT API / hosted MCP deployments
- [ ] **Client allowlist explicit**: populate `ALLOWED_CLIENT_IDS` for every production client, or set `AUTH_ALLOW_ANY_CLIENT_ID=true` only as a documented broad-access decision
- [ ] **JWT secret strong**: `JWT_SECRET_KEY` is 32+ random characters
- [ ] **Debug mode off**: `DEBUG_PAYLOADS=0` in production

---

## Common Configuration Mistakes

### 1. Wrong Telegram User ID

**Symptom**: Bot replies "Access denied" when you message it

**Fix**:

```bash
# Message @userinfobot on Telegram to get your user ID
# Then update .env:
ALLOWED_USER_IDS=123456789
```

### 2. Invalid API Keys

**Symptom**: All summaries fail with "401 Unauthorized" or "Invalid API key"

**Fix**: Regenerate keys at:

- Firecrawl: https://firecrawl.dev/account
- OpenRouter: https://openrouter.ai/keys

### 3. Mixing LLM Providers

**Symptom**: Bot starts but summaries fail with "Model not found"

**Fix**: Ensure provider and API key match:

```bash
# For OpenRouter
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-...

# For OpenAI
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...

# For Anthropic
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
```

### 4. Redis Connection Failures

**Symptom**: Warning logs about Redis but bot still works

**Fix**: Redis is optional. Either:

- Start Redis server: `docker run -d -p 6379:6379 redis:7-alpine`
- Or disable: `REDIS_ENABLED=false`

### 5. YouTube ffmpeg Missing

**Symptom**: YouTube downloads fail with "ffmpeg not found"

**Fix**:

```bash
# macOS
brew install ffmpeg

# Ubuntu/Debian
sudo apt-get install ffmpeg

# Docker (already included in image)
```

---

## Environment Variable Precedence

Variables are loaded in this order (later overrides earlier):

1. **Default values** (in `app/config/settings.py`)
2. **System environment** (`export VAR=value`)
3. **`.env` file** (in project root or specified via `--env-file`)
4. **CLI arguments** (for CLI tools only, e.g., `--log-level DEBUG`)

**Best Practice**: Use `.env` file for all configuration (easier to manage and version-control-friendly).

---

## Testing Your Configuration

```bash
# Validate environment variables are loaded correctly
python -c "from app.config.settings import RuntimeConfig; config = RuntimeConfig(); print('Config loaded successfully!')"

# Test Firecrawl connection
curl -H "Authorization: Bearer $FIRECRAWL_API_KEY" \
     -X POST https://api.firecrawl.dev/v1/scrape \
     -H "Content-Type: application/json" \
     -d '{"url":"https://example.com"}' | jq .

# Test OpenRouter connection
curl -H "Authorization: Bearer $OPENROUTER_API_KEY" \
     -X POST https://openrouter.ai/api/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{
       "model": "deepseek/deepseek-v4-flash",
       "messages": [{"role": "user", "content": "Hello"}]
     }' | jq .

# Test Redis connection (if enabled)
redis-cli -u $REDIS_URL ping

# Test Qdrant connection (if enabled)
curl "$QDRANT_URL/healthz"
```

---

## Related Documentation

- [Quickstart Tutorial](../guides/quickstart.md) - Step-by-step setup guide
- [FAQ § Configuration](../explanation/faq.md#configuration) - Common configuration questions
- [TROUBLESHOOTING § Configuration](troubleshooting.md#configuration-issues) - Fix config problems
- [DEPLOYMENT.md](../guides/deploy-production.md) - Production deployment guide

---

**Last Updated**: 2026-03-28

**Found an error or have a question?** [Open an issue](https://github.com/po4yka/ratatoskr/issues) or check [FAQ](../explanation/faq.md).
