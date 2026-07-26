# Scraper Chain

How Ratatoskr extracts clean article content from arbitrary URLs: the provider taxonomy, fallback logic, deployment topology, quality gates, and configuration recipes.

**Audience:** Contributors and operators who need to understand, tune, or extend the content-extraction pipeline. **Type:** Explanation. **Related:** [`docs/explanation/architecture-overview.md`](architecture-overview.md) (parent), [`docs/reference/environment-variables.md`](../reference/environment-variables.md) (full env-var reference), [`docs/explanation/faq.md`](../explanation/faq.md) (operational tips). **Source:** [`app/adapters/content/scraper/chain.py`](../../app/adapters/content/scraper/chain.py), [`factory.py`](../../app/adapters/content/scraper/factory.py), [`protocol.py`](../../app/adapters/content/scraper/protocol.py), [`reddit_provider.py`](../../app/adapters/content/scraper/reddit_provider.py), [`hn_provider.py`](../../app/adapters/content/scraper/hn_provider.py), [`scrapling_provider.py`](../../app/adapters/content/scraper/scrapling_provider.py), [`crawl4ai_provider.py`](../../app/adapters/content/scraper/crawl4ai_provider.py), [`defuddle_provider.py`](../../app/adapters/content/scraper/defuddle_provider.py), [`firecrawl_provider.py`](../../app/adapters/content/scraper/firecrawl_provider.py), [`playwright_provider.py`](../../app/adapters/content/scraper/playwright_provider.py), [`crawlee_provider.py`](../../app/adapters/content/scraper/crawlee_provider.py), [`direct_html_provider.py`](../../app/adapters/content/scraper/direct_html_provider.py), [`scrapegraph_provider.py`](../../app/adapters/content/scraper/scrapegraph_provider.py), [`webwright_provider.py`](../../app/adapters/content/scraper/webwright_provider.py).

---

## Overview

`ContentScraperChain` implements the generic extraction port used by the summarize graph's `extract` node. It holds an ordered list of provider instances and tries each in turn until one returns substantive, high-quality content. Every provider implements `ContentScraperProtocol` and returns a `FirecrawlResult`, so downstream code never needs to know which provider served the request; the `endpoint` field names the winner (or `"chain"` on total failure). Results are persisted to `crawl_results` regardless of outcome.

---

## Provider taxonomy

| Provider | Tier | Default position | Self-hosted requirement |
| --- | --- | --- | --- |
| `reddit` | platform API | 1 | None — URL-scoped Reddit public JSON fetch |
| `hn` | platform API | 2 | None — URL-scoped Algolia HN item API fetch |
| `scrapling` | in-process | 3 | None — pure Python (curl_cffi / Playwright via DynamicFetcher) |
| `direct_pdf` | in-process | 4 | None — direct PDF download + PyMuPDF extraction |
| `crawl4ai` | Docker sidecar | 5 | `crawl4ai` container at `SCRAPER_CRAWL4AI_URL` |
| `firecrawl_self_hosted` | Docker sidecar | 6 | `firecrawl-api` stack at `FIRECRAWL_SELF_HOSTED_URL` |
| `defuddle` | Docker sidecar | 7 | `defuddle-api` container at `SCRAPER_DEFUDDLE_API_BASE_URL` |
| `cloakbrowser` | browser sidecar (CDP) | 8 | `cloakbrowser` container at `SCRAPER_CLOAKBROWSER_URL` (Playwright `connect_over_cdp`) |
| `playwright` | browser pool (in-process) | 9 | Chromium installed via `playwright install chromium` |
| `crawlee` | browser pool (in-process) | 10 | Chromium (same as playwright) |
| `direct_html` | in-process | 11 | None — raw httpx fetch + trafilatura |
| `scrapegraph_ai` | in-process (LLM-driven) | 12 | `scrapegraphai` package + valid `OPENROUTER_API_KEY` |
| `webwright` | Docker sidecar (LLM-driven browser agent) | 13 | `webwright` container at `WEBWRIGHT_URL` + non-empty `WEBWRIGHT_HOST_ALLOWLIST` |

Provider positions 1 and 2 are URL-scoped: `reddit` only supports Reddit comment URLs and `hn` only supports Hacker News item URLs. Unsupported URLs skip those providers before attempt recording and fall through to the generic chain. Provider position 6 (`firecrawl`) is active only when `FIRECRAWL_SELF_HOSTED_ENABLED=true`; cloud Firecrawl is not used for article scraping. Position 12 (`scrapegraph_ai`) is active only when `scrapegraphai` is installed and `OPENROUTER_API_KEY` is set. Position 13 (`webwright`) is the absolute last-resort tier: runs a Microsoft Webwright (https://github.com/microsoft/Webwright) browser-agent loop per URL at ~10-30× the cost of a normal scrape and only fires when `WEBWRIGHT_ENABLED=true` *and* the URL's host appears in `WEBWRIGHT_HOST_ALLOWLIST`. An empty allowlist short-circuits provider construction so the sidecar is never reached. See [Webwright](webwright.md) for the design rationale and the three integration paths (scraper rung, `/browse` Telegram command, and the `WebwrightEnricher` enrichment service).

Position 8 (`cloakbrowser`) is the stealth-browser rung — an [upstream CloakHQ/CloakBrowser](https://github.com/CloakHQ/CloakBrowser) sidecar in `cloakserve` CDP mode that drives a Chromium build with C++ source-level fingerprint patches (canvas, WebGL, GPU, WebRTC, UA). It runs under the `with-scrapers` Docker profile and is reached over the internal Docker network only. The upstream binary is licensed for use but not redistribution, so we always pull the upstream image rather than rebake it — pinned in `ops/docker/docker-compose.yml` by multi-arch manifest digest (`@sha256:...`) so a re-pushed tag cannot silently swap the binary. `CLOAKBROWSER_AUTO_UPDATE=false` is set inside the container for the same reproducibility reason. When the sidecar is absent (no `with-scrapers` profile) the provider build still appears in the chain but the per-call connection fails fast, and the chain falls through to in-process `playwright` exactly as it does today when `crawl4ai` is down.

### Stealth configuration

The C++ fingerprint patches in cloakserve apply automatically over CDP, but several stealth knobs do not. Our provider closes the gap per request:

- **Per-domain fingerprint seed.** Each scrape appends `?fingerprint=<seed>&timezone=<tz>&locale=<loc>` to the CDP URL. The seed is `sha1(registrable_domain)[:12]`, so repeated scrapes of the same host reuse the same in-cloakserve Chrome process (and the same fingerprint, like a returning user would), while distinct hosts spawn distinct processes and distinct fingerprints. Without this, every request shares cloakserve's default seed — a detection signal in itself.
- **Timezone/locale rotation.** Selected from a 4-entry pool (`UTC`/`en-US`, `Europe/Berlin`/`de-DE`, `Asia/Tokyo`/`ja-JP`, `America/Sao_Paulo`/`pt-BR`) indexed by the same seed. Removes the giveaway "every request from this IP claims UTC + en-US."
- **Humanize over CDP.** Upstream's `humanize=True` is a Python-wrapper feature that does not cross the CDP boundary. The provider probes for an importable `cloakbrowser.human.patch_page` helper first, and if absent falls back to an in-house bezier mouse-move + wheel sequence (under ~200 ms) so behavioral signals look non-mechanical to Cloudflare/Turnstile scoring. Disable with `SCRAPER_CLOAKBROWSER_HUMANIZE=false`.
- **Optional proxy.** Set `SCRAPER_CLOAKBROWSER_PROXY=http://...` or `socks5://...` to forward a single proxy URL via the per-request `?proxy=` query param. No per-request rotation or proxy-pool data structure — opt-in single proxy only.

Every successful scrape stamps the seed, timezone, locale, humanize path (`patched` / `in_house` / `skipped`), and whether a proxy was configured (boolean only, never the URL itself) into `crawl_results.options_json` so failures correlate back to the exact stealth config that ran. The `/health` diagnostics surface the same flags except the proxy URL — that stays out of the unauthenticated endpoint to avoid leaking embedded credentials.

---

## Chain execution and fallback

```mermaid
flowchart TD
    Start(["URL reaches graph extract node"]) --> JsCheck{"JS-heavy host?\nSCRAPER_JS_HEAVY_HOSTS"}
    JsCheck -- yes --> Reorder[Browser providers reordered to front]
    JsCheck -- no --> Reddit

    Reorder --> Reddit

    subgraph InProcess[In-process]
        Reddit[1. reddit\nReddit URLs only]
        HN[2. hn\nHN item URLs only]
        Scrapling[3. scrapling]
        DirectPDF[4. direct_pdf]
        DirectHTML[11. direct_html]
        ScrapegraphAI[12. scrapegraph_ai]
    end

    subgraph Sidecars[Docker sidecars]
        Crawl4AI[5. crawl4ai]
        Firecrawl[6. firecrawl_self_hosted]
        Defuddle[7. defuddle]
        CloakBrowser[8. cloakbrowser]
    end

    subgraph BrowserPool[Browser pool]
        Playwright[9. playwright]
        Crawlee[10. crawlee]
    end

    Reddit --> RedditGate{URL supported\nand content OK?}
    RedditGate -- unsupported / fail --> HN
    RedditGate -- success --> Success

    HN --> HNGate{URL supported\nand content OK?}
    HNGate -- unsupported / fail --> Scrapling
    HNGate -- success --> Success

    Scrapling --> ScraplingGate{Content OK?}
    ScraplingGate -- error page --> DirectPDF
    ScraplingGate -- too short / low-value --> DirectPDF
    ScraplingGate -- success --> Success

    DirectPDF --> DirectPDFGate{Content OK?}
    DirectPDFGate -- fail --> Crawl4AI
    DirectPDFGate -- success --> Success

    Crawl4AI --> Crawl4AIGate{Content OK?}
    Crawl4AIGate -- fail --> Firecrawl
    Crawl4AIGate -- success --> Success

    Firecrawl --> FirecrawlGate{Content OK?}
    FirecrawlGate -- fail --> Defuddle
    FirecrawlGate -- success --> Success

    Defuddle --> DefuddleGate{Content OK?}
    DefuddleGate -- fail --> CloakBrowser
    DefuddleGate -- success --> Success

    CloakBrowser --> CloakBrowserGate{Content OK?}
    CloakBrowserGate -- fail --> Playwright
    CloakBrowserGate -- success --> Success

    Playwright --> PlaywrightGate{Content OK?}
    PlaywrightGate -- fail --> Crawlee
    PlaywrightGate -- success --> Success

    Crawlee --> CrawleeGate{Content OK?}
    CrawleeGate -- fail --> DirectHTML
    CrawleeGate -- success --> Success

    DirectHTML --> DirectHTMLGate{Content OK?}
    DirectHTMLGate -- fail --> ScrapegraphAI
    DirectHTMLGate -- success --> Success

    ScrapegraphAI --> ScrapegraphGate{Content OK?}
    ScrapegraphGate -- fail --> Webwright[13. webwright\nhost-allowlisted only]
    ScrapegraphGate -- success --> Success

    Webwright --> WebwrightGate{Content OK?}
    WebwrightGate -- fail / not allowlisted --> Exhausted([FirecrawlResult\nstatus=ERROR\nendpoint=chain])
    WebwrightGate -- success --> Success

    Success([FirecrawlResult persisted\nto crawl_results])

    classDef inproc fill:#d4e6f1,stroke:#2e86c1
    classDef sidecar fill:#d5f5e3,stroke:#1e8449
    classDef browser fill:#fdebd0,stroke:#d35400
    classDef terminal fill:#f9ebea,stroke:#cb4335

    class Scrapling,DirectHTML,ScrapegraphAI inproc
    class Crawl4AI,Firecrawl,Defuddle,CloakBrowser,Webwright sidecar
    class Playwright,Crawlee browser
    class Exhausted terminal
```

Each rung applies the quality gates described in the next section before deciding whether to return or continue to the next provider. "Content OK" means the result passed all gates; any gate failure advances the chain.

---

## Deployment topology

```mermaid
flowchart LR
    subgraph RatatoskrContainer[Ratatoskr container]
        Chain[ContentScraperChain]
        Scrapling2[Scrapling\nin-process]
        DirectHTML2[direct_html\nin-process]
        ScrapegraphAI2[scrapegraph_ai\nin-process]
        Chromium[Local Chromium\nfor playwright + crawlee]
        Chain --> Scrapling2
        Chain --> DirectHTML2
        Chain --> ScrapegraphAI2
        Chain --> Chromium
    end

    subgraph Crawl4AIService[crawl4ai container]
        C4AI[crawl4ai\nport 11235]
    end

    subgraph DefuddleService[defuddle-api container]
        DefuddleAPI[defuddle-api\nport 3003\nops/docker/defuddle/]
    end

    subgraph FirecrawlStack[firecrawl stack]
        FirecrawlAPI[firecrawl-api\nport 3002]
        FirecrawlPW[firecrawl-playwright]
        FirecrawlRedis[firecrawl-redis]
        FirecrawlPG[firecrawl-postgres]
        FirecrawlRMQ[firecrawl-rabbitmq]
        FirecrawlAPI --- FirecrawlPW
        FirecrawlAPI --- FirecrawlRedis
        FirecrawlAPI --- FirecrawlPG
        FirecrawlAPI --- FirecrawlRMQ
    end

    subgraph External[External]
        OpenRouter[OpenRouter API\nhttps://openrouter.ai\nscrapegraph_ai only]
    end

    Chain -- "SCRAPER_CRAWL4AI_URL\n→ crawl4ai:11235" --> C4AI
    Chain -- "FIRECRAWL_SELF_HOSTED_URL\n→ firecrawl-api:3002" --> FirecrawlAPI
    Chain -- "SCRAPER_DEFUDDLE_API_BASE_URL\n→ defuddle-api:3003" --> DefuddleAPI
    Chain -- "WEBWRIGHT_URL\n→ webwright:8090\n(host-allowlisted only)" --> WebwrightAPI[webwright sidecar\nMicrosoft Webwright\nbrowser-agent loop]
    ScrapegraphAI2 -- "OPENROUTER_API_KEY\n(last resort only)" --> OpenRouter
    WebwrightAPI -- "OPENAI_BASE_URL\n(OpenRouter compat)" --> OpenRouter
```

All sidecar connections are optional: a provider that cannot reach its sidecar returns an error result and the chain continues. The `scrapegraph_ai` and `webwright` providers are the only ones that contact an external endpoint (OpenRouter), and only as a last resort. The `webwright` sidecar is double-gated: by the chain's host allowlist (no allowlist match → no sidecar call) and by the compose profile `with-webwright` (sidecar not running → provider build returns `None`).

### Operator surfaces (Crawl4AI sidecar)

- **Interactive playground** — `http://crawl4ai:11235/playground` lets you test crawl requests against the live sidecar from a browser UI.
- **Real-time dashboard** — `http://crawl4ai:11235/dashboard` shows active crawl jobs, queue depth, and resource usage.

---

## Quality gates per rung

`ContentScraperChain.scrape_markdown` applies these gates to every provider result before deciding to accept or fall through:

- **`_is_error_page`** — regex match against HTTP error patterns (403, 404, 401, "access denied", Russian equivalents) on bodies shorter than 1 500 characters. Short bodies matching a pattern are rejected.
- **`min_content_length`** — rejects content shorter than the configured threshold (default 400 chars; controlled by `SCRAPER_MIN_CONTENT_LENGTH`). Applied only when `min_content_length > 0`.
- **`detect_low_value_content`** — quality filter that scores character count, word count, and content signal density. Applied when `min_content_length > 0`.
- **JS-heavy reorder** — when the request URL matches a host in `SCRAPER_JS_HEAVY_HOSTS`, browser providers (`playwright`, `crawlee`) are moved to the front of the effective provider list before any rung is tried.
- **SSRF preflight** — user-submitted target URLs are checked before provider delivery. Localhost, RFC1918/private networks, link-local/metadata ranges, reserved ranges, DNS results that resolve to blocked ranges, and non-http(s) schemes are rejected. `SCRAPER_ALLOW_PRIVATE_NETWORK_URLS=true` is only for isolated local development and does not allow metadata, link-local, reserved, or non-http(s) targets.

Providers that delegate fetching to sidecars or third-party libraries still cannot enforce redirect-hop checks inside that delegated runtime. The chain blocks obvious dangerous initial targets before calling those providers; backend-controlled `httpx` providers additionally perform per-hop redirect checks with connection-time DNS pinning.

---

## Anti-fingerprinting

The browser-based providers (`playwright`, `crawlee`) and `scrapling`'s `DynamicFetcher` path all integrate browser fingerprint randomization following the same design as `apify/fingerprint-suite`. `PlaywrightProvider` generates a randomized user-agent and viewport on each request. `CrawleeProvider` uses a `DefaultFingerprintGenerator` that rotates headers, viewport dimensions, and platform strings. `ScraplingProvider`'s `DynamicFetcher` (Playwright-based) inherits Scrapling's built-in TLS and browser fingerprint impersonation. The goal is to avoid consistent browser signatures that would be blocked by bot-detection middleware.

---

## Configuration recipes

### Single provider for testing

Force all requests through one provider; the chain ignores the rest.

```env
SCRAPER_FORCE_PROVIDER=crawl4ai
```

### Custom provider order biased toward Firecrawl first

Override the default order. Providers not listed are not instantiated.

```env
SCRAPER_PROVIDER_ORDER=firecrawl,scrapling,crawl4ai,defuddle,playwright,crawlee,direct_html
```

Note: `firecrawl` in the order only activates when `FIRECRAWL_SELF_HOSTED_ENABLED=true`; without it the factory skips that slot and the effective order shifts up.

### Disable the LLM rung

`scrapegraph_ai` is already excluded from the chain when `OPENROUTER_API_KEY` is unset. To disable it explicitly when the key is present:

```env
SCRAPER_SCRAPEGRAPH_ENABLED=false
```

---

## Cross-references

- Parent architecture: [`docs/explanation/architecture-overview.md`](architecture-overview.md)
- All scraper env vars: [`docs/reference/environment-variables.md`](../reference/environment-variables.md)
- Operational tips: [`docs/explanation/faq.md`](../explanation/faq.md)
- Source — chain: [`app/adapters/content/scraper/chain.py`](../../app/adapters/content/scraper/chain.py)
- Source — factory: [`app/adapters/content/scraper/factory.py`](../../app/adapters/content/scraper/factory.py)
- Source — protocol: [`app/adapters/content/scraper/protocol.py`](../../app/adapters/content/scraper/protocol.py)
