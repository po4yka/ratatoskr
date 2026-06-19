# Enable Web Search Enrichment

Add real-time web context to article summaries using web search.

**Audience:** Users, Operators **Difficulty:** Beginner **Estimated Time:** 3 minutes

---

## What is Web Search Enrichment?

Web search enrichment uses an LLM to:

1. **Analyze content** and identify knowledge gaps (unfamiliar entities, recent events, claims)
2. **Extract search queries** (max 3) if additional context would help
3. **Search the web** via Firecrawl Search API or DuckDuckGo
4. **Inject results** into summarization prompt for up-to-date context

**When it helps:**

- News articles (time-sensitive topics)
- Research papers (need latest findings)
- Tutorial articles (check if still relevant)

**When to skip:**

- Timeless content (classic literature, historical docs)
- Privacy-sensitive content (internal docs, private blogs)
- Cost-sensitive usage (adds ~500 tokens + 1-3 search API calls per summary)

---

## Prerequisites

- Ratatoskr installed and running
- Firecrawl cloud API key (`FIRECRAWL_API_KEY`) for the Search API path, or DuckDuckGo (no key required). Note: `FIRECRAWL_API_KEY` is used exclusively by this web-search enrichment path; it is not part of the article-extraction scraper chain. See [`docs/explanation/scraper-chain.md`](../explanation/scraper-chain.md) for how article extraction works.

---

## Steps

### 1. Enable Web Search

Add to your `.env` file:

```bash
# Enable web search enrichment
WEB_SEARCH_ENABLED=true

# Max search queries per article (default: 3)
WEB_SEARCH_MAX_QUERIES=3

# Search timeout (default: 10s)
WEB_SEARCH_TIMEOUT_SEC=10

# Injected context size cap (default: 2000 chars)
WEB_SEARCH_MAX_CONTEXT_CHARS=2000
```

---

### 2. Tune Search Behavior (Optional)

```bash
# Only trigger search for sufficiently rich content
WEB_SEARCH_MIN_CONTENT_LENGTH=500

# Cache search context to reduce repeated lookups
WEB_SEARCH_CACHE_TTL_SEC=3600
```

---

### 3. Restart Bot

```bash
# Docker
docker restart ratatoskr

# Local
# Press Ctrl+C to stop, then:
python bot.py
```

---

## Verification

Send an article URL about a recent event:

```
https://example.com/article-about-recent-event
```

**Expected behavior:**

1. Bot replies "📥 Processing article..."
2. If web search triggered, you'll see: "🔍 Enriching with web context..."
3. Summary includes up-to-date information beyond LLM training cutoff

**How to tell if web search was used:**

- Check logs for "Web search triggered" or "Web search skipped"
- Enable `DEBUG_PAYLOADS=1` to see search queries and results

---

## Cost Impact

Web search adds:

- **LLM tokens:** ~500 tokens per article (analysis + query extraction)
- **Search API calls:** 1-3 calls per article (only when triggered)
- **Total extra cost:** ~$0.01 per summary

**Optimization:** Only ~30-40% of articles trigger web search (self-contained content is skipped).

Monitor actual trigger rate with `ratatoskr_web_search_decisions_total{decision="executed"}` and separate enrichment LLM spend with `ratatoskr_openrouter_cost_usd_total{purpose="web_search"}`. Query breadth is visible through `ratatoskr_web_search_query_results`, a histogram of articles returned per web-search query.

---

## Troubleshooting

### Web search never triggers

**Symptom:** No "🔍 Enriching with web context..." message

**Causes:**

1. **Content is self-contained** (LLM decides search wouldn't help)
2. **Web search disabled** in config

**Solution:**

```bash
# Verify enabled
grep WEB_SEARCH_ENABLED .env
# Should show: WEB_SEARCH_ENABLED=true

# Enable debug logging to see LLM decision
LOG_LEVEL=DEBUG
docker restart ratatoskr

# Check logs
docker logs ratatoskr | grep "Web search"
```

---

### Search API errors

**Symptom:** Error message "Web search failed" or "Search API error"

- Verify `FIRECRAWL_API_KEY` is valid
- Check Firecrawl credit balance
- Reduce `WEB_SEARCH_MAX_QUERIES` and/or increase `WEB_SEARCH_TIMEOUT_SEC`

---

### Too many search queries

**Symptom:** High API costs from excessive search

**Solution:**

```bash
# Reduce max queries
WEB_SEARCH_MAX_QUERIES=1
WEB_SEARCH_MAX_CONTEXT_CHARS=1200

# Or disable for certain content types
# (Not yet implemented, manual disable/enable for now)
```

---

## Advanced Configuration

### Customize Search Behavior

```bash
# Search timeout (seconds)
WEB_SEARCH_TIMEOUT_SEC=10

# Minimum content length before search is considered
WEB_SEARCH_MIN_CONTENT_LENGTH=500

# Cache search results
WEB_SEARCH_CACHE_TTL_SEC=3600

# Enable Redis-backed caching for repeated queries
REDIS_ENABLED=true
```

---

## When to Use Web Search

### ✅ Good Use Cases

- **News articles**: Recent events, breaking news, current affairs
- **Tech tutorials**: Check if libraries/APIs still work
- **Research papers**: Cross-reference latest findings
- **Product reviews**: Verify claims, check for recalls
- **Historical articles**: Add recent developments

### ❌ Skip Web Search For

- **Timeless content**: Classic literature, philosophy, historical docs
- **Private/internal docs**: Company wikis, internal blogs (privacy risk)
- **Math/theory**: Self-contained content that doesn't change
- **Personal notes**: No web context needed
- **Cost-sensitive usage**: Disable if minimizing API costs

---

## Disable Web Search Temporarily

```bash
# In .env
WEB_SEARCH_ENABLED=false

# Restart bot
docker restart ratatoskr
```

Or use per-request override (future feature):

```
/summarize --no-web-search https://example.com
```

---

## Monitoring Web Search Usage

```bash
# Check how often web search triggers
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c "
  SELECT
    count(*) AS total_summaries,
    count(*) FILTER (WHERE web_search_triggered) AS with_search,
    round(
      100.0 * count(*) FILTER (WHERE web_search_triggered) / nullif(count(*), 0),
      2
    ) AS trigger_rate_pct
  FROM summaries
  WHERE created_at > now() - interval '30 days';
"

# Check search query costs
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c "
  SELECT
    request_id,
    web_search_queries,
    web_search_results_count
  FROM summaries
  WHERE web_search_triggered
  ORDER BY created_at DESC
  LIMIT 10;
"
```

---

## See Also

- [FAQ § Web Search](../explanation/faq.md#web-search)
- [TROUBLESHOOTING § Web Search Issues](../reference/troubleshooting.md)
- [environment_variables.md § Web Search](../reference/environment-variables.md)
- [multi-agent-architecture.md](../explanation/multi-agent-architecture.md) - WebSearchAgent design

---

**Last Updated:** 2026-02-09
