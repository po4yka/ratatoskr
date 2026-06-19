# Optional YAML Configuration

`ratatoskr.yaml` is the Phase 1 home for power-user settings. Keep first-run secrets in `.env`; use YAML for scraper tuning, OpenRouter model choices, YouTube, Twitter/X, MCP, monitoring-adjacent settings, and other optional behavior.

## Search Order

Ratatoskr loads the first file found:

1. `RATATOSKR_CONFIG`, when set
2. `./ratatoskr.yaml`
3. `./config/ratatoskr.yaml`
4. `/app/config/ratatoskr.yaml`

Merge precedence is:

```
non-secret YAML  >  os.environ  >  .env / ctor args  >  defaults
secret env       >  defaults    (YAML secret keys are dropped and logged)
```

`config/ratatoskr.yaml` is the operator's authoritative on-disk config; `.env` carries secrets only. Fields marked as secrets in `app/config/_secret_marker.py` are stripped from YAML at load time and logged as `yaml_secret_keys_ignored` — place those in `.env` instead. Deprecated env vars fail startup with an actionable message.

## Minimal `.env`

Only these values are required for the Telegram bot plus default OpenRouter LLM path:

```env
API_ID=123456
API_HASH=replace_with_telegram_api_hash
BOT_TOKEN=1234567890:replace_with_botfather_token_secret
ALLOWED_USER_IDS=123456789
OPENROUTER_API_KEY=sk-or-replace_with_openrouter_key
```

`JWT_SECRET_KEY` is required only when web/API/browser-extension JWT auth is enabled. Generate it with `openssl rand -hex 32`. If you set `LLM_PROVIDER=openai`, `anthropic`, or `ollama`, replace the OpenRouter secret with the matching direct provider key and model settings; see [Configure LLM Provider](../guides/configure-llm-provider.md).

## Example `ratatoskr.yaml`

```yaml
runtime:
  log_level: INFO
  request_timeout_sec: 60
  preferred_lang: auto
  max_concurrent_calls: 4
  llm_provider: openrouter

openrouter:
  model: deepseek/deepseek-v4-flash
  fallback_models:
    - qwen/qwen3.5-plus-02-15
    - moonshotai/kimi-k2-0905
  flash_model: qwen/qwen3.5-flash-02-23

scraper:
  profile: balanced
  provider_order:
    - scrapling
    - crawl4ai
    - firecrawl
    - defuddle
    - playwright
    - crawlee
    - direct_html
    - scrapegraph_ai
  defuddle_enabled: true
  firecrawl_self_hosted_enabled: false

firecrawl:
  api_key: ""
  timeout_sec: 90
  wait_for_ms: 3000

youtube:
  enabled: true
  storage_path: /data/videos
  preferred_quality: 1080p
  subtitle_languages:
    - en
    - ru

twitter:
  enabled: false
  prefer_firecrawl: true
  playwright_enabled: false

signal_ingestion:
  enabled: true
  max_items_per_source: 30
  hn_enabled: true
  hn_feeds:
    - top
    - best
  reddit_enabled: true
  reddit_subreddits:
    - selfhosted
    - python
  reddit_listing: hot
  reddit_requests_per_minute: 60
  twitter_enabled: false
  twitter_ack_cost: false
  social_x_ingestion_enabled: false
  social_x_timeline_mode: user_posts
  social_threads_ingestion_enabled: false

mcp:
  enabled: false
  transport: stdio
```

## Notes

- Supported summarization backends are `openrouter`, `openai`, `anthropic`, and `ollama`; `openrouter` remains the default and most feature-complete runtime path. See [LLM Providers](llm-providers.md) for the feature matrix.
- Use OpenRouter model IDs such as `openai/...`, `anthropic/...`, `google/...`, or `deepseek/...` in the `openrouter` section when `LLM_PROVIDER=openrouter`. Use direct provider model names in the `openai`, `anthropic`, or `ollama` sections when selecting those adapters.
- The `with-cloud-ollama` Compose profile is a reachability/experimentation helper for Ollama-compatible deployments; production summarization uses it only when `LLM_PROVIDER=ollama` and `ollama.base_url` points at the reachable endpoint.
- The default scraper chain order is Reddit API → Hacker News Algolia → Scrapling → direct PDF → Crawl4AI → Firecrawl → Defuddle → CloakBrowser → Playwright → Crawlee → direct HTML → Scrapegraph-AI → Webwright. Reddit and Hacker News are URL-scoped and skipped before attempts for unrelated hosts. Each provider is skipped when its sidecar is unavailable or its enabled flag is false. See [`docs/explanation/scraper-chain.md`](../explanation/scraper-chain.md) for the full chain reference.
- The `firecrawl` provider slot activates only when `scraper.firecrawl_self_hosted_enabled: true`; cloud Firecrawl is not used for article scraping. `FIRECRAWL_API_KEY` is only consumed by the web-search enrichment path (`TopicSearchService`), not by the scraper chain.
- SSRF redirect enforcement is strongest for backend-controlled HTTP fetchers that use the centralized safe httpx transport and manual redirect loops: proxy image fetches, direct HTML, Defuddle, and Crawl4AI sidecar requests re-check each redirect target and block private, link-local, localhost, and metadata IP ranges. Third-party/browser-controlled providers have limits: Scrapling, Playwright, Crawlee, Firecrawl sidecars, and ScrapeGraphAI may resolve or follow redirects inside external runtimes where this process cannot pin DNS at connect time, so keep those runtimes isolated from internal networks and treat their URL filters as preflight/best-effort controls.
- Defuddle defaults to enabled (`scraper.defuddle_enabled: true`) but requires a reachable `defuddle-api` sidecar (default: `http://defuddle-api:3003`). The sidecar can be replaced by the public `https://defuddle.md` API for development by setting `SCRAPER_DEFUDDLE_API_BASE_URL=https://defuddle.md`.
- The `with-scrapers` Docker Compose profile starts an in-compose self-hosted Firecrawl stack at `http://firecrawl-api:3002`. Set `scraper.firecrawl_self_hosted_enabled: true` to use it; self-hosted Firecrawl takes precedence when both self-hosted and cloud Firecrawl are configured.
- Signal ingestion optional sources are disabled unless `signal_ingestion.enabled` and the per-source flag are both true. Hacker News uses the official Firebase API and has no credentials. Reddit uses public subreddit JSON with a default 60 requests/minute guard, below the free-tier 100 requests/minute ceiling. Substack is handled as RSS via `/feed`; use existing RSS subscription flows. Authenticated X and Threads social feed ingestion additionally requires active connected accounts and `social_x_ingestion_enabled` / `social_threads_ingestion_enabled`.
- Twitter/X extraction is optional and should stay disabled unless explicitly needed. The legacy generic X/Twitter proactive placeholder remains disabled by default and requires explicit `twitter_ack_cost: true` / `TWITTER_INGESTION_ACK_COST=true`; authenticated connected-account X ingestion is separately gated by `social_x_ingestion_enabled` and uses existing OAuth connections rather than a raw token setting.
