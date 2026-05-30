"""Canonical ratatoskr.* OTel span attribute keys.

All span.set_attribute() calls across the codebase must reference these
constants rather than inline strings. The module has zero runtime
dependencies and is safe to import anywhere.

Grouping mirrors the five instrumentation phases in
docs/explanation/observability-instrumentation-plan.md:
  Phase 1 - Scraper chain
  Phase 2 - LLM token/cost
  Phase 3 - Request root span / correlation
  Phase 4 - Agent layer
  Phase 5 - Embedding / Qdrant / concurrency
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Phase 3 -- Request / Correlation
# ---------------------------------------------------------------------------

# The application-level correlation ID carried on every DB row and log line.
# Reuses the existing ID; do NOT introduce a parallel trace ID.
REQUEST_CORRELATION_ID = "ratatoskr.correlation_id"

# SHA-256 of the normalised URL (computed by app.core.url_utils.compute_dedupe_hash).
# Used for cache-hit span events on the url_flow.cache_hit child span.
REQUEST_DEDUPE_HASH = "ratatoskr.request.dedupe_hash"

# Coarse source classification for the root url_flow.process span.
# Values: "url" | "youtube" | "twitter" | "academic" | "forward" | "rss" | "unknown"
REQUEST_SOURCE_TYPE = "ratatoskr.request.source_type"

# ---------------------------------------------------------------------------
# Phase 3 -- Telegram intake root span  (telegram.update)
# ---------------------------------------------------------------------------

# One of: "url" | "forward" | "command" | "unknown"
TELEGRAM_INTERACTION_TYPE = "ratatoskr.telegram.interaction_type"

# Boolean-as-string: "true" | "false"
TELEGRAM_HAS_FORWARD = "ratatoskr.telegram.has_forward"

# Telegram chat ID as a string (avoid integer span attribute for uniformity).
TELEGRAM_CHAT_ID = "ratatoskr.telegram.chat_id"

# Source type as surfaced by prepare() -- mirrors REQUEST_SOURCE_TYPE semantics.
TELEGRAM_SOURCE_TYPE = "ratatoskr.telegram.source_type"

# ---------------------------------------------------------------------------
# Phase 1 -- Scraper chain  (scraper.chain and scraper.<name> spans)
# ---------------------------------------------------------------------------

# Name of the winning provider on chain success.
# Set on the parent scraper.chain span by the existing chain code.
SCRAPER_WINNER = "ratatoskr.scraper.winner"

# Total provider attempts made in a chain invocation (integer).
SCRAPER_ATTEMPTS = "ratatoskr.scraper.attempts"

# Byte length of the content returned by the winning provider.
SCRAPER_CONTENT_LEN = "ratatoskr.scraper.content_len"

# Provider name on the per-rung scraper.<name> span.
# Values: "scrapling" | "crawl4ai" | "firecrawl" | "defuddle" |
#         "playwright" | "crawlee" | "direct_html" | "scrapegraph_ai"
SCRAPER_PROVIDER = "ratatoskr.scraper.provider"

# Execution mode of the chain invocation.
# Values: "serial" | "tiered_race"
SCRAPER_MODE = "ratatoskr.scraper.mode"

# Tier number (integer) the rung belongs to within a tiered-race invocation.
SCRAPER_TIER = "ratatoskr.scraper.tier"

# Per-rung timeout budget in seconds (float).
SCRAPER_TIMEOUT_SEC = "ratatoskr.scraper.timeout_sec"

# Correlation ID threaded down to the per-rung scraper.<name> span.
# Matches REQUEST_CORRELATION_ID; repeated here for the scraper namespace.
SCRAPER_REQUEST_ID = "ratatoskr.scraper.request_id"

# The URL being scraped (set on the chain-level span).
# Mirrors SOURCE_URL; defined here so the scraper chain can use it without
# depending on Phase 4/5 constants.
SCRAPER_URL = "ratatoskr.scraper.url"

# Outcome of a single provider attempt within the chain.
# Values: "success" | "cancelled" | "error" | "error_page" | "too_short" |
#         "low_value" | "no_content"
SCRAPER_OUTCOME = "ratatoskr.scraper.outcome"

# ---------------------------------------------------------------------------
# Phase 2 -- LLM token / cost  (llm.chat and llm.chat_structured spans)
# ---------------------------------------------------------------------------

# Provider label (e.g. "openrouter", "openai", "anthropic").
LLM_PROVIDER = "ratatoskr.llm.provider"

# Requested model ID (before any fallback resolution).
LLM_MODEL = "ratatoskr.llm.model"

# Model actually used to serve the response (may differ from LLM_MODEL after
# OpenRouter's own upstream routing).
LLM_MODEL_SERVED = "ratatoskr.llm.model_served"

# Prompt tokens consumed by the call (integer).
LLM_TOKENS_PROMPT = "ratatoskr.llm.tokens_prompt"

# Completion tokens produced by the call (integer).
LLM_TOKENS_COMPLETION = "ratatoskr.llm.tokens_completion"

# Sum of prompt + completion tokens (integer; convenience attribute).
LLM_TOKENS_TOTAL = "ratatoskr.llm.tokens_total"

# Estimated cost in USD as a float (e.g. 0.000123).
LLM_COST_USD = "ratatoskr.llm.cost_usd"

# End-to-end latency of the call in milliseconds (float).
LLM_LATENCY_MS = "ratatoskr.llm.latency_ms"

# 0-based index of the fallback rung that produced the final result.
# 0 = primary model succeeded; 1+ = fallback activated.
LLM_FALLBACK_RUNG_INDEX = "ratatoskr.llm.fallback_rung_index"

# Total number of distinct models attempted across the fallback chain (integer).
LLM_MODELS_ATTEMPTED_COUNT = "ratatoskr.llm.models_attempted_count"

# Prompt cache read tokens (Anthropic / OpenRouter prompt-caching; integer).
LLM_CACHE_READ_TOKENS = "ratatoskr.llm.cache_read_tokens"

# Prompt cache creation tokens written to the provider cache (integer).
LLM_CACHE_CREATION_TOKENS = "ratatoskr.llm.cache_creation_tokens"

# ---------------------------------------------------------------------------
# Phase 4 -- Agent layer  (agent.<name> spans)
# ---------------------------------------------------------------------------

# Canonical agent name (e.g. "content_extraction", "validation",
# "web_search", "repo_analysis", "combined_summary").
AGENT_NAME = "ratatoskr.agent.name"

# 1-based attempt index within the self-correction / retry loop.
AGENT_ATTEMPT = "ratatoskr.agent.attempt"

# Validation failure reason string (set on agent.validation span).
AGENT_VALIDATION_FAILURE_REASON = "ratatoskr.agent.validation_failure_reason"

# ---------------------------------------------------------------------------
# Phase 3 / 4 -- Source classification
# ---------------------------------------------------------------------------

# Coarse source type for the content being processed.
# Values: "url" | "youtube" | "twitter" | "academic" | "forward" |
#         "rss" | "github" | "unknown"
SOURCE_TYPE = "ratatoskr.source.type"

# Normalised URL of the content being processed.
SOURCE_URL = "ratatoskr.source.url"

# ---------------------------------------------------------------------------
# Phase 5 -- Vector store / Embedding
# ---------------------------------------------------------------------------

# Qdrant / vector-store operation name.
# Values: "upsert" | "replace" | "delete" | "query" | "scroll"
VECTOR_OPERATION = "ratatoskr.vector.operation"

# Outcome of the vector-store operation.
# Values: "success" | "error" | "not_found"
VECTOR_STATUS = "ratatoskr.vector.status"

# Embedding provider / model identifier (e.g. "all-MiniLM-L6-v2",
# "models/text-embedding-004").
EMBEDDING_MODEL = "ratatoskr.embedding.model"

# Dimensionality of the produced embedding vector (integer).
EMBEDDING_DIMS = "ratatoskr.embedding.dims"

# Number of texts in a batch embedding call (integer; 1 for single encode).
EMBEDDING_BATCH_SIZE = "ratatoskr.embedding.batch_size"

# ---------------------------------------------------------------------------
# Phase 5 -- Concurrency / queue depth
# ---------------------------------------------------------------------------

# Snapshot queue depth sampled at span open (integer; informational).
QUEUE_DEPTH = "ratatoskr.queue.depth"

# ---------------------------------------------------------------------------
# Taskiq task spans
# ---------------------------------------------------------------------------

# Boolean indicating whether the task result was an error (maps to
# TaskiqResult.is_err).  Used by OtelMiddleware on the taskiq.* span.
TASK_IS_ERR = "ratatoskr.task.is_err"
