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
| `OPENROUTER_API_KEY` | OpenRouter API key for the default `LLM_PROVIDER=openrouter` path |

## [OPTIONAL] LLM Provider Selection

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `LLM_PROVIDER` | `openrouter` | Active LLM backend: `openrouter`, `openai`, `anthropic`, or `ollama` |
| `OPENAI_API_KEY` | _(empty)_ | OpenAI API key (when using `openai` provider) |
| `OPENAI_MODEL` | `gpt-4o` | OpenAI model name |
| `OPENAI_FALLBACK_MODELS` | `gpt-4o-mini` | Comma-separated fallback models |
| `OPENAI_ORGANIZATION` | _(none)_ | OpenAI organization ID |
| `OPENAI_ENABLE_STRUCTURED_OUTPUTS` | `true` | Enable structured output mode |
| `ANTHROPIC_API_KEY` | _(empty)_ | Anthropic API key (when using `anthropic` provider) |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-5-20250929` | Anthropic model name |
| `ANTHROPIC_FALLBACK_MODELS` | `claude-3-5-haiku-20241022` | Comma-separated fallback models |
| `ANTHROPIC_ENABLE_STRUCTURED_OUTPUTS` | `true` | Enable structured output mode |
| `OLLAMA_BASE_URL` | _(empty)_ | OpenAI-compatible Ollama/cloud endpoint base URL (when using `ollama` provider) |
| `OLLAMA_API_KEY` | _(empty)_ | API key or bearer token for the Ollama-compatible endpoint |
| `OLLAMA_MODEL` | `llama3.3` | Ollama-compatible model name |
| `OLLAMA_FALLBACK_MODELS` | _(empty)_ | Comma-separated fallback models |
| `OLLAMA_ENABLE_STRUCTURED_OUTPUTS` | `false` | Enable structured output mode for compatible hosted models |
| `OLLAMA_MAX_RESPONSE_SIZE_MB` | `10` | Max response payload size (MB) for the Ollama-compatible client |

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
| `SCRAPER_BROWSER_ENABLED` | `true` | Master switch for browser-based providers (`cloakbrowser`, `playwright`, `crawlee`) |
| `SCRAPER_FORCE_PROVIDER` | _(none)_ | Force single provider token (`scrapling`, `crawl4ai`, `firecrawl`, `defuddle`, `cloakbrowser`, `playwright`, `crawlee`, `direct_html`, `scrapegraph_ai`) |
| `SCRAPER_JS_HEAVY_HOSTS` | _(none)_ | CSV host list for JS-heavy heuristic overlays |
| `SCRAPER_MIN_CONTENT_LENGTH` | `400` | Minimum extracted text length to accept content |
| `SCRAPER_ALLOW_PRIVATE_NETWORK_URLS` | `false` | Local-development override for user-submitted localhost/RFC1918 targets. Leave disabled outside isolated dev; metadata, link-local, reserved, and non-http(s) targets remain blocked. |
| `SCRAPER_PROVIDER_ORDER` | `["scrapling", "direct_pdf", "crawl4ai", "firecrawl", "defuddle", "cloakbrowser", "playwright", "crawlee", "direct_html", "scrapegraph_ai"]` | Ordered list of scraping providers to try |
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
| `SCRAPER_CLOAKBROWSER_ENABLED` | `true` | Enable CloakBrowser CDP-sidecar provider (stealth Chromium via `cloakserve`) |
| `SCRAPER_CLOAKBROWSER_URL` | `http://cloakbrowser:9222` | CloakBrowser `cloakserve` HTTP endpoint; Playwright resolves the WebSocket debugger URL from `/json/version` on this host |
| `SCRAPER_CLOAKBROWSER_TIMEOUT_SEC` | `60` | CloakBrowser request timeout (seconds) |
| `SCRAPER_CLOAKBROWSER_HUMANIZE` | `true` | Apply post-connect humanize layer (bezier mouse/scroll pacing) so behavioral signals look human to Cloudflare/Turnstile scoring; disable to skip both the upstream `cloakbrowser.human` helper probe and the in-house bezier fallback |
| `SCRAPER_CLOAKBROWSER_PROXY` | _(empty)_ | Optional proxy URL (`http://...` or `socks5://...`) forwarded to cloakserve via the per-request `?proxy=` query param; never logged to `/health` (only `proxy_configured: true/false` is reported there) |
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
| `WEBWRIGHT_ENABLED` | `false` | Enable the Microsoft Webwright LLM-driven browser-agent provider (heaviest tier; runs ~10-30x the cost of a normal scrape). Default off. Requires the `webwright` Docker sidecar from `ops/docker/webwright/` to be running. |
| `WEBWRIGHT_URL` | `http://webwright:8090` | HTTP endpoint of the Webwright sidecar. |
| `WEBWRIGHT_HOST_ALLOWLIST` | _(empty)_ | CSV or list of hosts where Webwright is allowed to fire. Empty disables it; use `*` to allow any host (not recommended). Subdomain matches are automatic — listing `example.com` matches `www.example.com`. |
| `WEBWRIGHT_MAX_STEPS` | `20` | Maximum agent steps per Webwright invocation. |
| `WEBWRIGHT_TIMEOUT_SEC` | `180` | Wall-clock budget per Webwright invocation. |
| `WEBWRIGHT_MODEL` | `openai/gpt-4o-mini` | Model the sidecar passes to Webwright. Routed via OpenRouter using its OpenAI-compatible endpoint by default. |
| `WEBWRIGHT_OPENAI_BASE_URL` | `https://openrouter.ai/api/v1` | OpenAI-compatible endpoint the sidecar points Webwright at. Override to use OpenAI/Anthropic directly. |

**Notes**:

- Scrapling is a free, in-process scraper that requires no API key. It is tried first by default.
- Crawl4AI is a self-hosted Docker sidecar (`crawl4ai` service on port 11235). When the service is not reachable the provider is skipped automatically.
- Firecrawl now only supports self-hosted mode (`FIRECRAWL_SELF_HOSTED_ENABLED=true`). Cloud Firecrawl (`FIRECRAWL_API_KEY`) is no longer used by the article scraper chain; it remains available for the web-search enrichment subsystem.
- Defuddle is now enabled by default and points at the self-hosted Docker Compose service (`http://defuddle-api:3003`). Pointing it at `https://defuddle.md` logs a `defuddle_provider_cloud_url_deprecated` warning.
- CloakBrowser is a self-hosted stealth-Chromium sidecar reached over CDP (`cloakhq/cloakbrowser` running `cloakserve`); it ships under the `with-scrapers` Docker profile. The upstream binary is licensed for use but not redistribution — pull the upstream image, pinned in compose by **multi-arch manifest digest** (`@sha256:...`), and do not rebake. `CLOAKBROWSER_AUTO_UPDATE=false` is set inside the container to keep deploys reproducible. Per-request stealth knobs (per-domain fingerprint seed, timezone/locale rotation, humanize-over-CDP) are applied client-side by the provider; see `docs/explanation/scraper-chain.md#stealth-configuration` for the full set. When the sidecar is absent the per-call CDP connection fails fast and the chain falls through to in-process `playwright`.
- Playwright fallback is useful for JS-heavy pages that fail in HTTP-only extractors.
- Crawlee fallback is a single-page advanced fallback (BeautifulSoup stage, then Playwright stage); it is not broad multi-page site crawling in this pipeline.
- `direct_html` is a lightweight fallback using trafilatura for simple pages.
- ScrapeGraph-AI is the last-resort provider. It uses the OpenRouter API key and model to run an in-process LLM-driven scrape. Requires `pip install scrapegraphai`.
- Webwright is an even-heavier last-resort provider that runs an LLM-driven Playwright browser-agent loop ([microsoft/Webwright](https://github.com/microsoft/Webwright)) via a Docker sidecar (`ops/docker/webwright/`, compose profile `with-webwright`). Default off; requires both `WEBWRIGHT_ENABLED=true` and at least one host in `WEBWRIGHT_HOST_ALLOWLIST`. Empty allowlist short-circuits provider construction so the sidecar is never called.
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

## Transcription (CPU-only ASR)

Off by default. When enabled, ratatoskr can transcribe URLs, voice/audio/video_note messages, and (optionally) fill the audio-transcript slot in the YouTube pipeline when no native captions are available. Fully CPU-side, no GPU, no cloud API. Requires `ffmpeg` on `PATH` and the `transcription` optional extra (`pip install 'ratatoskr[transcription]'`).

Two language presets are wired in. Set `TRANSCRIPTION_LANGUAGE` and the right engine + tokenization mode are picked automatically:

- **`en` (default)** — Kroko English streaming Zipformer (Apache-2.0, ~80 MB INT8). Streaming backend, BPE tokens with the U+2581 word-start marker. Source: `csukuangfj/sherpa-onnx-streaming-zipformer-en-kroko-2025-08-06`.
- **`ru`** — GigaAM-v3 e2e RNN-T (MIT-licensed, ~230 MB INT8, ~8.4% WER on Russian benchmarks). Offline backend (consumes full audio before emitting text — there is no Russian streaming model in the sherpa-onnx ecosystem as of 2026-05), char-level Cyrillic tokens, **punctuation and text normalization baked into the model output**. Source: `Smirnov75/GigaAM-v3-sherpa-onnx`; original weights from `ai-sage/GigaAM-v3`.

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `TRANSCRIPTION_ENABLED` | `false` | Master switch. When `false`, the `/transcribe` command, voice auto-handler, and URL-pipeline fallback are all inactive and the transcription package is never loaded |
| `TRANSCRIPTION_LANGUAGE` | `en` | Primary language preset. `en` picks the streaming Kroko Zipformer; `ru` picks the offline GigaAM-v3 RNN-T |
| `TRANSCRIPTION_MODEL_PATH` | `/data/models/transcription` | Directory holding the chosen model. If empty on first use, the bundle for the configured language is auto-downloaded and upstream filenames are normalized to `encoder.onnx` / `decoder.onnx` / `joiner.onnx` / `tokens.txt`. If `tokens.txt` already exists, the directory is treated as a custom model and no download happens |
| `TRANSCRIPTION_BACKEND` | (auto) | Override the backend selected by `TRANSCRIPTION_LANGUAGE`. `streaming` uses `sherpa_onnx.OnlineRecognizer`; `offline_transducer` uses `OfflineRecognizer.from_transducer`. Leave unset unless you are loading a custom model that disagrees with its language preset |
| `TRANSCRIPTION_TOKENS_MODE` | (auto) | Override the tokens-mode selected by `TRANSCRIPTION_LANGUAGE`. `bpe` honours the U+2581 word-start marker; `char` joins tokens verbatim |
| `TRANSCRIPTION_SPEED` | `1.5` | Pre-transcription speedup (pitch preserved via ffmpeg `atempo`). 1.5x cuts CPU time by ~30% with minimal accuracy loss; use 1.0 for noisy / fast-speech sources |
| `TRANSCRIPTION_NUM_THREADS` | `2` | Threads sherpa-onnx may use for inference |
| `TRANSCRIPTION_MAX_DURATION_SEC` | `1800` | Refuse any media longer than this. Protects against runaway multi-hour transcribe jobs |
| `TRANSCRIPTION_AUTO_VOICE` | `true` | When `TRANSCRIPTION_ENABLED`, auto-transcribe forwarded voice / audio / video_note messages without requiring `/transcribe` |
| `TRANSCRIPTION_AUTO_URL_PIPELINE` | `false` | When `TRANSCRIPTION_ENABLED`, fill `VideoSourceRequest.audio_transcript_text` in the YouTube pipeline when both `youtube-transcript-api` and VTT subtitles return empty. Opt-in because it adds CPU cost to every captionless video |
| `TRANSCRIPTION_DIARIZATION_ENABLED` | `false` | Add speaker labels (`SPEAKER_00`, `SPEAKER_01`, ...). Downloads two additional ONNX models on first use. Note: diarization needs per-sentence timestamps, which the offline RU backend does not always emit — diarization on Russian audio may degrade to plain text without speaker labels |
| `TRANSCRIPTION_DIARIZATION_MODEL` | `pyannote` | Segmentation model. `pyannote` is CC-BY-4.0 (default, safe for most uses). `reverb` is more accurate but distributed under a **non-commercial** license — review the Rev.ai model card before commercial use |
| `TRANSCRIPTION_DIARIZATION_PATH` | `/data/models/diarization` | Directory holding the diarization segmentation + embedding models |
| `TRANSCRIPTION_EMBEDDING_MODEL` | `3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx` | Filename of the speaker-embedding ONNX in the sherpa-onnx `speaker-recongition-models` release (note upstream typo) |
| `TRANSCRIPTION_DIARIZATION_CLUSTER_THRESHOLD` | `0.5` | FastClustering threshold for auto speaker-count detection. Higher = fewer merged speakers |
| `TRANSCRIPTION_DEFAULT_NUM_SPEAKERS` | `-1` | Default speaker count (`-1` = auto-detect; auto-detection degrades above ~7 speakers) |

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
| `DATABASE_POOL_TIMEOUT_SECONDS` | `30.0` | Seconds to wait for a free pooled connection before `TimeoutError` (SQLAlchemy `QueuePool.pool_timeout`) |
| `DATABASE_PREPARED_STATEMENT_CACHE_SIZE` | `100` | asyncpg prepared-statement cache size per connection. Set to `0` to disable caching if `cached plan must not change result type` errors appear (transaction-pooling proxy, or varying IN-list churn after migrations) |
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

Configures scheduled nulling of raw artifact columns and cleanup of orphaned local artifacts. Summaries, search metadata, cost, status, and request rows are never purged by these settings. A TTL of `0` disables purge for that subsystem.

| Variable | Type | Default | Description |
|---|---|---|---|
| `RETENTION_ENABLED` | bool | `true` | Master switch. Set to `false` to disable all purge runs. |
| `RETENTION_CRON` | str | `"0 3 * * *"` | UTC cron for the daily purge job (3 am UTC). |
| `RETENTION_BATCH_SIZE` | int | `500` | Max rows updated per subsystem per run. Next run continues the backlog. |
| `RETENTION_PRIVACY_NO_RETENTION_MODE` | bool | `false` | Best-effort privacy mode. New crawl and LLM write paths skip avoidable raw prompt/content payload persistence, and the next purge run immediately nulls raw fields while preserving summaries/search metadata. |
| `RETENTION_TELEGRAM_RAW_DAYS` | int | `30` | Days to keep `telegram_messages` raw columns (`text_full`, `entities_json`, `telegram_raw_json`). `0` = never purge. |
| `RETENTION_RAW_EXTRACTED_CONTENT_DAYS` / `RETENTION_CRAWL_CONTENT_DAYS` | int | `7` | Days to keep `crawl_results` raw extracted content columns (`content_markdown`, `content_html`, raw provider JSON, metadata, links). `0` = never purge. |
| `RETENTION_LLM_PROMPT_RESPONSE_DAYS` / `RETENTION_LLM_PAYLOAD_DAYS` | int | `90` | Days to keep `llm_calls` request/response columns. Cost, token, model, status, and latency fields are always preserved. `0` = never purge. |
| `RETENTION_LLM_PROMPT_RESPONSE_POLICY` | str | `"full"` | `full` stores LLM prompt/response payloads until their TTL; `metadata_only` stores only cost/token/model/status/latency/error metadata for new calls. |
| `RETENTION_VIDEO_TRANSCRIPT_DAYS` | int | `30` | Days to keep `video_downloads.transcript_text`. `0` = never purge. |
| `RETENTION_DOWNLOADED_MEDIA_DAYS` | int | `30` | Days to keep downloaded video, subtitle, metadata, and thumbnail files referenced by `video_downloads`. The database row remains, but path and size fields are nulled after cleanup. `0` = never purge. |
| `RETENTION_EXPORT_TEMP_FILE_HOURS` | int | `24` | Hours to keep orphaned export temp files under the private `ratatoskr-exports` temp directory. Normal successful responses still delete their own temp file immediately. `0` = never purge. |
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

These knobs govern how long the generic LLM response workflow spends per provider call and per model attempt. OpenRouter uses the full fallback ladder and per-model budget enforcement; OpenAI, Anthropic, and Ollama clients accept the same protocol kwargs so the workflow can pass them safely, even when a provider ignores unsupported streaming or timeout details.

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `LLM_CALL_TIMEOUT_SEC` | `420.0` | Total wall-clock budget for one LLM call (across the full fallback ladder) |
| `LLM_PER_MODEL_TIMEOUT_MIN_SEC` | `90.0` | Minimum per-model budget. Per-model timeout is `max(this, LLM_CALL_TIMEOUT_SEC / num_models)` so slow models in long ladders are not starved |
| `LLM_PER_MODEL_TIMEOUT_OVERRIDES` | _(empty)_ | Comma-separated `model=seconds` overrides, e.g. `moonshotai/kimi-k2.5=180,minimax/minimax-m1=240`. Overrides win over the formula above. Malformed entries are skipped with a warning |
| `LLM_CALL_MAX_RETRIES` | `2` | Retries on transient HTTP failures inside a single model attempt |

**Effective timeout can exceed `LLM_CALL_TIMEOUT_SEC`.** The outer `asyncio.timeout()` wrapper is expanded to fit the full cascade so the per-model floor is never starved:

```
per_model_timeout    = max(LLM_PER_MODEL_TIMEOUT_MIN_SEC, LLM_CALL_TIMEOUT_SEC / num_models)
effective_timeout    = max(LLM_CALL_TIMEOUT_SEC, num_models * per_model_timeout + 15s)   # 15s inter-model buffer; 0 when num_models == 1
```

With the defaults and a 5-model ladder this yields `max(420, 5*90 + 15) = 465s`, i.e. 45s beyond the configured 420s. This is intentional (a coherent answer from one slow model beats a guaranteed-fast cascade of timeouts). Whenever the effective timeout exceeds the configured value the workflow emits a `llm_effective_timeout_expanded` WARNING with the full derivation, so the expansion is never silent. Lower `LLM_PER_MODEL_TIMEOUT_MIN_SEC` or shorten the fallback ladder if you need the effective ceiling closer to `LLM_CALL_TIMEOUT_SEC`.

## LLM Usage Budgets

These knobs bound LLM spend for self-hosted deployments. The per-request token limit caps the workflow `max_tokens` sent to providers and is also recorded when persisted usage exceeds the configured limit. Daily and monthly hard budgets are enforced before workflow LLM calls using persisted `llm_calls.cost_usd`; soft budgets are exposed as warnings in the owner-only admin cost endpoint.

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `LLM_MAX_TOKENS_PER_REQUEST` | _(none)_ | Maximum prompt plus completion tokens allowed per persisted LLM call, and maximum generated tokens requested by summary workflows |
| `LLM_MAX_COST_USD_PER_REQUEST` | _(none)_ | Maximum estimated USD cost allowed per persisted LLM call when provider cost data is available |
| `LLM_DAILY_SOFT_BUDGET_USD` | _(none)_ | Daily cost warning budget |
| `LLM_MONTHLY_SOFT_BUDGET_USD` | _(none)_ | Monthly cost warning budget |
| `LLM_BUDGET_WARNING_THRESHOLD_RATIO` | `0.8` | Ratio of a configured soft budget that starts reporting warning status |
| `LLM_DAILY_HARD_BUDGET_USD` | _(none)_ | Daily persisted LLM cost at which new workflow LLM calls are blocked |
| `LLM_MONTHLY_HARD_BUDGET_USD` | _(none)_ | Monthly persisted LLM cost at which new workflow LLM calls are blocked |

## Mixed-Source Aggregation Rollout

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `AGGREGATION_BUNDLE_ENABLED` | `true` | Master switch for bundle aggregation in Telegram and API |
| `AGGREGATION_ROLLOUT_STAGE` | `enabled` | Availability stage: `disabled`, `internal`, `owner_beta`, `enabled` |
| `AGGREGATION_META_EXTRACTORS_ENABLED` | `true` | Enable dedicated Threads/Instagram extraction instead of generic article fallback |
| `AGGREGATION_ARTICLE_MEDIA_ENABLED` | `true` | Attach curated article/X image assets to aggregation documents and multimodal summary requests |
| `AGGREGATION_NON_YOUTUBE_VIDEO_ENABLED` | `true` | Enable shared Telegram/Meta video normalization with transcript/audio/OCR fallbacks |

## Social Integrations

These variables configure connected social-account OAuth clients used by the Mobile API social-auth routes and the Telegram `/social`, `/connect_x`, `/connect_threads`, `/connect_instagram`, and `/disconnect_social <provider>` commands. Instagram configuration currently supports the read-only Instagram API with Instagram Login scaffold documented in `docs/reference/social-integrations.md`; it does not enable private-feed access or replace the unauthenticated Meta scraper fallback.

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `SOCIAL_X_INGESTION_ENABLED` | `false` | Enable authenticated X timeline ingestion when `SIGNAL_INGESTION_ENABLED=true` and the user has an active X connection |
| `SOCIAL_X_TIMELINE_MODE` | `user_posts` | Authenticated X timeline mode: `user_posts` uses `GET /2/users/:id/tweets`; `home_timeline` uses `GET /2/users/:id/timelines/reverse_chronological` |
| `SOCIAL_THREADS_INGESTION_ENABLED` | `false` | Enable authenticated Threads `GET /me/threads` ingestion when `SIGNAL_INGESTION_ENABLED=true` and the user has an active Threads connection |
| `INSTAGRAM_CLIENT_ID` | _(none)_ | Instagram App ID for Business Login for Instagram |
| `INSTAGRAM_CLIENT_SECRET` | _(none)_ | Instagram App Secret; stored as `SecretStr`, never logged |
| `INSTAGRAM_REDIRECT_URI` | _(none)_ | Redirect URI registered for Instagram Login |
| `INSTAGRAM_SCOPES` | `instagram_business_basic` | Read-only Instagram professional-account profile/media scope; publish, messaging, and moderation scopes are intentionally rejected |
| `INSTAGRAM_GRAPH_BASE_URL` | `https://graph.instagram.com/v25.0` | Instagram Graph API base URL for profile and media reads |

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

## X Integration

Local X/Twitter bookmark sync via the host-side `x_bookmarks-cli` (`ft`). The container reads the read-only `ft` SQLite database on a Taskiq schedule and ingests new bookmarks into `requests` + `x_bookmark_metadata`. See `docs/explanation/x-bookmarks-integration.md` for the full design. Configuration owner: `app/config/x_bookmarks.py::XBookmarksConfig`.

| Variable | Default | Required | Description | Used by |
|----------|---------|----------|-------------|---------|
| `X_BOOKMARKS_SYNC_ENABLED` | `true` | No | Master switch for the Taskiq bookmark delta-scan job; when `false`, the job is not registered with the scheduler | `app/tasks/scheduler.py` |
| `X_BOOKMARKS_SYNC_CRON` | `*/15 * * * *` | No | UTC cron expression for the bookmark delta-scan job (default: every 15 minutes) | `app/tasks/scheduler.py` |
| `X_BOOKMARKS_DB_PATH` | `/x_bookmarks/bookmarks.db` | No | Path to the read-only `ft` SQLite bookmarks database inside the container; typically the mount target of `~/.fieldtheory/bookmarks.db` on the host | `app/adapters/ingestors/x_bookmarks_ingestor.py` |

**Notes:**

- The host runs `ft sync` on its own schedule (typically hourly via launchd/systemd). The container-side delta-scan picks up host syncs and manual `ft sync` gestures within one `X_BOOKMARKS_SYNC_CRON` interval.
- `bookmarks.db` must be mounted read-only into the container (`/x_bookmarks/bookmarks.db:ro`); the ingestor opens it with `aiosqlite` in URI read-only mode and never writes.
- `X_BOOKMARKS_SYNC_ENABLED=false` disables only the scheduled Taskiq job; nothing else in the pipeline references the x_bookmarks mount.

---

## Git Mirror Backup (gitout)

Periodic bare-clone mirroring of GitHub repositories (starred, owned, watched) and arbitrary extra repos to a local directory using the gitout engine. The `git_mirrors` DB table is the primary source of repos to mirror; `GIT_BACKUP_EXTRA_REPOS` supplements it. Configuration owner: `app/config/git_backup.py::GitBackupConfig`.

**Token reuse**: `GITHUB_TOKEN_ENCRYPTION_KEY` (documented in [GitHub Integration](#github-integration)) is reused to decrypt per-user GitHub tokens for authenticated mirror clones. When absent, GitHub mirrors fall back to unauthenticated clones; no separate key variable is needed.

| Variable | Type | Default | Description |
|---|---|---|---|
| `GIT_BACKUP_ENABLED` | bool | `false` | Master switch for the periodic git-backup Taskiq job; when `false` the job is not registered with the scheduler. |
| `GIT_BACKUP_SYNC_CRON` | str | `"0 4 * * *"` | UTC 5-field cron expression for the mirror sync job (default: 04:00 UTC daily). |
| `GIT_BACKUP_DATA_PATH` | str | `/data/git-mirrors` | Writable directory where bare git clones are stored; typically a bind-mounted or named Docker volume on the worker service. |
| `GIT_BACKUP_WORKERS` | int (1–32) | `4` | Number of parallel git clone/fetch workers. |
| `GIT_BACKUP_REPO_TIMEOUT_SECONDS` | int | `3600` | Per-repository operation timeout in seconds. |
| `GIT_BACKUP_FETCH_LFS` | bool | `false` | Fetch Git LFS objects during mirror operations. |
| `GIT_BACKUP_MAINTENANCE_STRATEGY` | str | `gc-auto` | Post-fetch maintenance strategy applied to each mirror. Accepted values: `gc-auto`, `geometric`, `none`. |
| `GIT_BACKUP_FULL_REPACK_INTERVAL` | str | `never` | How often to perform a full repack of each mirror. Accepted values: `never`, `weekly`, `monthly`. |
| `GIT_BACKUP_WRITE_COMMIT_GRAPH` | bool | `true` | Write a commit-graph file after each mirror update for faster graph walks. |
| `GIT_BACKUP_LARGE_REPO_THRESHOLD_KB` | int | `512000` | Repository disk size in KB above which large-repo handling applies (extended timeout, reduced parallelism). |
| `GIT_BACKUP_LARGE_REPO_TIMEOUT_MULTIPLIER` | int | `3` | Multiplier applied to `GIT_BACKUP_REPO_TIMEOUT_SECONDS` for repos that exceed the large-repo threshold. |
| `GIT_BACKUP_LARGE_REPO_MAX_PARALLEL` | int | `2` | Maximum number of large repos mirrored concurrently. |
| `GIT_BACKUP_MAX_CONSECUTIVE_FAILURES` | int | `5` | Number of consecutive failures before a repo is flagged as failing and subject to the cooldown policy. |
| `GIT_BACKUP_FAILURE_COOLDOWN_HOURS` | int | `24` | Hours to wait before retrying a repo that has exceeded `GIT_BACKUP_MAX_CONSECUTIVE_FAILURES`. |
| `GIT_BACKUP_AUTO_SKIP_FAILING` | bool | `true` | Automatically skip repos that are in the failure-cooldown window instead of retrying them every run. |
| `GIT_BACKUP_EXTRA_REPOS` | dict[str,str] | `{}` | Mapping of short name → clone URL for repos that should be mirrored but do not have a `git_mirrors` DB row (e.g. `{"my-project": "https://github.com/user/my-project.git"}`). Parsing a nested dict from a flat env var is awkward; prefer the `git_mirrors` DB table for dynamic configuration and reserve this field for static, deployment-time overrides via `ratatoskr.yaml`. |
| `GIT_BACKUP_SSL_CA_INFO` | str \| None | `None` | Path to a custom CA bundle (PEM) passed to git via `http.sslCAInfo`. When set, git uses this bundle to verify TLS certificates instead of its compiled-in CA store. Useful when mirroring from servers signed by a private or internal CA. When unset (default), no flag is injected. |
| `GIT_BACKUP_HTTP_VERSION` | str | `HTTP/1.1` | HTTP protocol version passed to git via `http.version`. Accepted values: `HTTP/1.1` (default, matching gitout's default) or `HTTP/2`. When `HTTP/2`, git may negotiate HTTP/2 via TLS ALPN. The per-run `force_http1` flag (set by the retry policy on `HTTP2_ERROR` failures) always overrides this setting. |
| `GIT_BACKUP_REPACK_WINDOW` | int | `50` | Value for git repack's `--window` option during full repacks (default: 50, matching gitout). Higher values improve pack density at the cost of more CPU. Must be >= 1. Only used when `GIT_BACKUP_FULL_REPACK_INTERVAL` is not `never`. |
| `GIT_BACKUP_REPACK_DEPTH` | int | `50` | Value for git repack's `--depth` option during full repacks (default: 50, matching gitout). Higher values improve pack density at the cost of more CPU. Must be >= 1. Only used when `GIT_BACKUP_FULL_REPACK_INTERVAL` is not `never`. |
| `GIT_BACKUP_CIRCUIT_BREAKER_THRESHOLD` | int | `3` | Number of consecutive `STORAGE_ERROR` failures that trip the storage circuit breaker and abort the remainder of the sync run (default: 3, matching gitout). Once tripped the breaker stays open for the current run and resets on the next. Must be >= 1. |
| `GIT_BACKUP_PREFLIGHT_TIMEOUT_SECONDS` | float | `10.0` | Timeout in seconds for the preflight storage write/read/delete sentinel check that runs before each sync (default: 10.0 s). If the check takes longer than this the entire sync is aborted with a storage error. Must be > 0. |
| `GIT_BACKUP_VERIFY_CERTIFICATES` | bool | `true` | When `false`, passes `http.sslVerify=false` to git, disabling TLS certificate verification. Mirrors gitout `ssl.verify_certificates`. Only disable on private infrastructure with a known-good CA. |
| `GIT_BACKUP_POST_BUFFER_SIZE` | int | `524288000` | Value for git's `http.postBuffer` in bytes (500 MB). Mirrors gitout `http.post_buffer_size`. Increase for repos that fail with `RPC failed; HTTP 411` on large pushes. |
| `GIT_BACKUP_LOW_SPEED_LIMIT` | int | `1000` | Value for git's `http.lowSpeedLimit` in bytes/second. Mirrors gitout `http.low_speed_limit`. Set to `0` to disable low-speed detection. |
| `GIT_BACKUP_LOW_SPEED_TIME` | int | `60` | Value for git's `http.lowSpeedTime` in seconds. Mirrors gitout `http.low_speed_time`. Only effective when `GIT_BACKUP_LOW_SPEED_LIMIT > 0`. |
| `GIT_BACKUP_SINGLE_BRANCH_ONLY` | bool | `false` | When `true`, uses `git clone --bare --single-branch` instead of `git clone --mirror`. Mirrors gitout `github.clone.single_branch_only`. Reduces disk usage for repos with many branches but omits all non-default refs. |
| `GIT_BACKUP_SHALLOW_CLONE_THRESHOLD_KB` | int | `0` | Repository size in KB above which a shallow clone (`--depth=1`) is used instead of a full mirror clone. `0` = disabled (opt-in). Gitout's default is 2 000 000 KB (2 GB). Only applies to initial clones. |
| `GIT_BACKUP_SHALLOW_CLONE_AFTER_FAILURES` | int | `0` | Consecutive failure count after which a shallow clone is attempted instead of a full mirror clone. `0` = disabled (opt-in). Gitout's default is 3. When both this and `GIT_BACKUP_SHALLOW_CLONE_THRESHOLD_KB` are non-zero, both conditions must be met. Only applies to initial clones. |
| `GIT_BACKUP_MIRROR_STARRED` | bool | `false` | When `true`, enumerate all starred repositories for each user with an active GitHub integration (`GET /user/starred`) and upsert a `git_mirrors` row per repo. Clone URLs use the HTTPS form `https://github.com/<owner>/<name>.git`. `size_kb` is populated from the GitHub-reported repo size so large-repo timeout scaling applies on the first clone. Disabled by default. |
| `GIT_BACKUP_MIRROR_OWNED` | bool | `false` | When `true`, enumerate all repositories owned by each user with an active GitHub integration (`GET /user/repos?affiliation=owner`) and upsert a `git_mirrors` row per repo. Clone URLs use the HTTPS form `https://github.com/<owner>/<name>.git`. `size_kb` is populated from the GitHub-reported repo size. Disabled by default. |
| `GIT_BACKUP_MIRROR_WATCHED` | bool | `false` | When `true`, enumerate all repositories watched by each user with an active GitHub integration (`GET /user/subscriptions`) and upsert a `git_mirrors` row per repo. Clone URLs use the HTTPS form `https://github.com/<owner>/<name>.git`. `size_kb` is populated from the GitHub-reported repo size. Disabled by default. |
| `GIT_BACKUP_MIRROR_GISTS` | bool | `false` | When `true`, enumerate all gists for each user with an active GitHub integration and upsert a `git_mirrors` row (source=`github`) per gist so it is cloned by the regular mirror sync. Gist clone URLs use the form `https://gist.github.com/<id>.git`. Disabled by default. |
| `GIT_BACKUP_INDEX_READMES` | bool | `false` | When `true`, index the README of each successfully-synced mirror with `repository_id IS NULL` (manual/arbitrary targets) into Qdrant after each sync run, enabling semantic search via `GET /v1/git-mirrors/search`. Requires the embedding service (`EMBEDDING_PROVIDER`) and Qdrant vector store (`QDRANT_URL`) to be configured. Indexing is best-effort and never blocks or fails the backup sync. GitHub-linked mirrors (with `repository_id IS NOT NULL`) are already searchable via the repository search endpoint and are excluded from this indexing path. |
| `GIT_BACKUP_RECONCILE_READMES` | bool | `false` | When `true`, after each sync run reconcile git_mirror README vectors in Qdrant against the database: delete orphaned points (deleted, excluded, or now-GitHub-linked mirrors) and recreate missing points (force re-index, or clear the index columns when the bare clone is gone from disk). Uses the same embedding + Qdrant infra as `GIT_BACKUP_INDEX_READMES`. Detection is also available standalone via the reconcile CLI. Best-effort; never blocks or fails the backup sync. |
| `GIT_BACKUP_PRUNE_EXCLUDED_DAYS` | int | `0` | When > 0, mirrors with `status=EXCLUDED` whose `excluded_at` is older than this many days are automatically pruned during each sync run: Qdrant point deleted (best-effort), on-disk bare clone removed (best-effort, only if `mirror_path` resolves strictly inside `GIT_BACKUP_DATA_PATH`), DB row deleted. `0` = disabled. The sweep runs after `perform_sync` and never blocks or fails the task. |
| `GIT_BACKUP_HC_PING_URL` | str \| None | `None` | Base Healthchecks.io (or compatible) ping URL for the sync job (e.g. `https://hc-ping.com/<uuid>`). When set, the task POSTs to `{url}/start` before the sync, to `{url}` on success, and to `{url}/fail` on exception. When empty or unset, health pinging is disabled. |
| `GIT_BACKUP_HC_PING_TIMEOUT_SECONDS` | float | `10.0` | HTTP timeout in seconds for each Healthchecks.io ping request. |

**Notes:**

- `GIT_BACKUP_ENABLED=false` disables only the scheduled Taskiq job; the underlying gitout engine and DB table remain available for manual invocation.
- `GIT_BACKUP_DATA_PATH` must be writable by the worker container user. Mount it as a named volume or bind mount in `ops/docker/docker-compose.yml` under the `worker` service.
- `GIT_BACKUP_MAINTENANCE_STRATEGY=gc-auto` runs `git gc --auto` after each fetch (low overhead, suitable for most deployments). `geometric` uses `git maintenance run --task=gc` with geometric repacking. `none` skips maintenance entirely (fastest per-fetch, but pack fragmentation accumulates).
- Large-repo handling activates when the on-disk bare clone exceeds `GIT_BACKUP_LARGE_REPO_THRESHOLD_KB`. Effective timeout becomes `GIT_BACKUP_REPO_TIMEOUT_SECONDS * GIT_BACKUP_LARGE_REPO_TIMEOUT_MULTIPLIER`, and at most `GIT_BACKUP_LARGE_REPO_MAX_PARALLEL` such repos are mirrored at once regardless of `GIT_BACKUP_WORKERS`.
- The failure-cooldown window is per-repo and tracked in the `git_mirrors` table. `GIT_BACKUP_AUTO_SKIP_FAILING=true` means a repo in cooldown is silently skipped; set to `false` to have it retried every run (useful for debugging transient failures).
- `GIT_BACKUP_VERIFY_CERTIFICATES=false` is a global override for all mirrors; there is no per-mirror SSL override. Only use on fully private deployments; disabling TLS verification exposes clones to MITM attacks.
- Shallow-clone (`GIT_BACKUP_SHALLOW_CLONE_THRESHOLD_KB` / `GIT_BACKUP_SHALLOW_CLONE_AFTER_FAILURES`) only applies to the initial `git clone`, never to `git remote update`. When both thresholds are configured, gitout's AND semantics apply: the repo must exceed the size threshold AND have at least the configured consecutive failures. The chosen strategy (`"shallow"` or `"full"`) is persisted to the `clone_strategy` column of `git_mirrors` so it is queryable.

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

Two separate chains apply depending on whether a field is secret-marked (see `app/config/_secret_marker.py`):

**Non-secret fields** (operational tunables — models, timeouts, scraper settings, etc.):

```
non-secret YAML  >  os.environ  >  .env / ctor args  >  defaults
```

**Secret fields** (API keys, tokens, credentials, PII):

```
secret env (os.environ / .env)  >  defaults
```

YAML values for secret-marked fields are dropped at load time and logged as `yaml_secret_keys_ignored`. Place all secrets in `.env` only.

`config/ratatoskr.yaml` is the operator's authoritative on-disk config for non-secret tunables; it is opt-in (missing file is silently skipped). `.env` carries secrets only. See [`docs/reference/config-file.md`](config-file.md) for the YAML search order and a full example.

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
