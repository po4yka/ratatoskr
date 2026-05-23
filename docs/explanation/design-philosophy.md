# Design Philosophy

Understanding the principles and trade-offs behind Ratatoskr's architecture.

**Audience:** Developers, Architects **Type:** Explanation **Related:** [Hexagonal Architecture](architecture-overview.md#layering-quick-reference), [Architecture Overview](architecture-overview.md)

---

## Core Principles

### 1. Simplicity Over Flexibility

**Philosophy:** Make common cases trivial, complex cases possible.

**Applied:**

- Single-user access control (no multi-tenancy complexity)
- SQLite database (no distributed database complexity)
- Environment variables only (no runtime configuration UI)
- One bot per owner (no shared infrastructure)

**Trade-off:** Users running at scale must deploy multiple instances, but 99% of users benefit from zero operational complexity.

**Rationale:** Most users want a personal content summarization assistant, not a SaaS platform. Optimizing for the common case (personal use) delivers better UX than building for the edge case (enterprise scale).

---

### 2. Observability as a First-Class Citizen

**Philosophy:** If you can't debug it, you don't own it.

**Applied:**

- Correlation IDs on every request (`telegram_message.id` → `request.id` → `llm_call.request_id`)
- Full payload persistence (Telegram messages, Firecrawl responses, LLM calls)
- Structured logging with context (loguru JSON format)
- Debug mode with redacted sensitive data (`DEBUG_PAYLOADS=1`)

**Trade-off:** Higher storage costs (~10-20% overhead for audit logs), but debugging time reduced from hours to minutes.

**Rationale:** External API failures (Firecrawl rate limits, OpenRouter model outages, Telegram API changes) are inevitable. Correlation IDs enable self-service debugging without developer intervention.

See: [Observability Strategy](observability-strategy.md)

---

### 3. Strict Contracts, Flexible Implementation

**Philosophy:** Define clear boundaries, allow internal evolution.

**Applied:**

- **Summary JSON Contract:** 35+ required fields with validation (`app/core/summary_contract.py`)
- **Hexagonal Architecture:** Core domain independent of Telegram, Firecrawl, OpenRouter
- **Database Schema:** Explicit migrations, no runtime schema inference
- **API Contracts:** OpenAPI spec for Mobile API, gRPC protobuf definitions

**Trade-off:** More upfront design work, but refactoring is safe and fast.

**Rationale:** When LLMs hallucinate or external APIs change format, strict contracts catch errors immediately rather than silently corrupting data.

See: [Summary Contract Design](summary-contract-design.md)

---

### 4. Async-First Concurrency Model

**Philosophy:** Don't block on I/O, maximize throughput per resource.

**Applied:**

- Telethon async Telegram client
- httpx async HTTP client (Firecrawl, OpenRouter)
- Semaphore-based rate limiting (`MAX_CONCURRENT_CALLS`)
- Optional uvloop for 30-40% performance boost
- Background task scheduling (APScheduler)

**Trade-off:** Async code is harder to debug (stack traces are shallow), but throughput is 5-10x higher than sync code.

**Rationale:** Summarization is I/O-bound (waiting on Firecrawl and OpenRouter APIs), not CPU-bound. A single instance can serve 100+ requests/hour with <512MB RAM.

---

### 5. Fail-Fast with Graceful Degradation

**Philosophy:** Surface errors immediately, but degrade gracefully when possible.

**Applied:**

- **Fail-fast:** Missing required env vars → immediate startup failure
- **Graceful:** Redis connection failure → log warning, continue without cache
- **Fail-fast:** Invalid summary JSON → retry with error feedback (3x max)
- **Graceful:** YouTube transcript unavailable → fallback to video download
- **Fail-fast:** Invalid URL format → immediate user error message
- **Graceful:** OpenRouter primary model down → fallback to secondary models

**Rationale:** Configuration errors should be caught at startup, not hours later during production traffic. Optional features should degrade gracefully to avoid cascading failures.

---

### 6. Zero-Trust External APIs

**Philosophy:** All external dependencies are guilty until proven reliable.

**Applied:**

- Exponential backoff with jitter (Firecrawl, OpenRouter)
- Circuit breaker pattern for repeated failures
- Timeout on all HTTP requests (default 90s)
- JSON validation before persistence
- Rate limit headers respected (`X-RateLimit-Remaining`)
- Fallback chains (Firecrawl → Trafilatura → Spacy)
- Model fallback chains (primary → secondary → tertiary)

**Trade-off:** More code for error handling, but production uptime is 99.9%+.

**Rationale:** Firecrawl and OpenRouter are external SaaS services with rate limits, quotas, and occasional outages. The bot must remain functional despite third-party failures.

---

### 7. Data Sovereignty and Privacy

**Philosophy:** User data is sacred. No tracking, no sharing, no surprises.

**Applied:**

- No analytics, no telemetry to third parties
- No PII stored (only Telegram user IDs, no names/emails/phones)
- API keys never logged (redacted in debug mode)
- Local SQLite storage (no cloud database)
- Self-hosted deployment (user owns the data)
- Single-user access control (no data co-mingling)

**Trade-off:** No usage analytics for product improvement, but user trust is absolute.

**Rationale:** Content summaries may contain sensitive information (private research, company docs, personal notes). Users must have complete control over their data.

---

### 8. Explicit Over Implicit

**Philosophy:** Prefer verbose clarity over clever brevity.

**Applied:**

- **Explicit config:** 250+ environment variables (all documented)
- **Explicit validation:** Pydantic models for all API requests/responses
- **Explicit migrations:** SQL migration files (no auto-migrations)
- **Explicit dependencies:** requirements.txt with pinned versions
- **Explicit types:** mypy type checking (90%+ coverage)
- **Explicit errors:** Correlation IDs in all error messages

**Trade-off:** More boilerplate code, but onboarding is faster and bugs are easier to find.

**Rationale:** When something breaks at 2am, explicit code is self-documenting. Clever code requires the original author to debug.

---

## Architectural Decisions

### Why Hexagonal Architecture?

**Problem:** Tightly coupled Telegram bot logic made testing painful and feature additions risky.

**Solution:** Ports and Adapters pattern separates core business logic from external dependencies.

**Benefits:**

- Test core logic without mocking Telegram/Firecrawl/OpenRouter
- Swap LLM providers (OpenRouter → Anthropic → OpenAI) with zero domain code changes
- Add new interfaces (CLI runner, Mobile API, gRPC) without touching core
- Framework independence (Telegram adapter can change without domain changes)

See: [Hexagonal Architecture](architecture-overview.md#layering-quick-reference)

---

### Why Multi-Agent LLM Pipeline?

**Problem:** Single-agent summarization had 85% success rate due to JSON formatting errors and content extraction failures.

**Solution:** Specialized agents (ContentExtraction, Summarization, Validation, WebSearch) with self-correction loops.

**Benefits:**

- Success rate increased to 94%+ (Summarization agent retries with error feedback)
- Clear separation of concerns (extraction vs summarization vs validation)
- Parallel execution of independent agents (web search + content extraction)
- Agent-specific prompt engineering (extraction optimized for markdown cleaning, summarization for JSON schema adherence)

See: [Multi-Agent Architecture](multi-agent-architecture.md)

---

### Why Strict JSON Summary Contract?

**Problem:** Unstructured LLM output made downstream features (search, UI rendering, quality metrics) impossible.

**Solution:** 35+ field JSON schema enforced via validation and LLM prompt constraints.

**Benefits:**

- Type-safe rendering in Telegram (no missing fields)
- Semantic search across standardized fields (`key_ideas`, `entities`, `topic_tags`)
- Quality metrics (readability scores, confidence levels, hallucination risk)
- Multi-language support (same schema in English and Russian)
- API client reliability (no "maybe this field exists" logic)

See: [Summary Contract Design](summary-contract-design.md)

---

### Why Single-User Access Control?

**Problem:** Multi-user systems require auth, authorization, user management, billing, quotas.

**Solution:** Hardcoded `ALLOWED_USER_IDS` whitelist. One bot = one owner.

**Benefits:**

- Zero auth complexity (no JWT, no OAuth, no sessions)
- Perfect cost control (no surprise bills from other users)
- Zero multi-tenancy bugs (no "user A sees user B's data")
- Instant deployment (no user registration flow)

**Trade-off:** Cannot share bot with friends without adding them to whitelist and restarting.

**Rationale:** For 99% of users, sharing means "deploy a second bot for your friend" (trivial with Docker). The 1% who need multi-user can fork and add proper auth.

See: [FAQ § Security](faq.md#security)

---

## Technology Choices

### Why SQLite Over PostgreSQL?

**Chosen:** SQLite (single-file database) **Rejected:** PostgreSQL, MySQL, MongoDB

**Rationale:**

- **Simplicity:** Zero setup (no server, no connection pooling, no backups)
- **Performance:** Sufficient for single-user workload (<1000 requests/day)
- **Portability:** Entire database is one file (easy backups, migrations)
- **Cost:** Zero hosting cost (included in Docker image)

**Trade-off:** No horizontal scaling, but single-user workload never exceeds SQLite's capabilities.

**When to reconsider:** If multi-user support is added, PostgreSQL becomes necessary for concurrent writes.

---

### Why Firecrawl Over Trafilatura?

**Chosen:** Firecrawl (SaaS content extraction) **Rejected:** Trafilatura, Newspaper3k, custom Playwright solution

**Rationale:**

- **JavaScript handling:** Firecrawl executes JavaScript, handles dynamic content
- **Success rate:** 95%+ extraction success vs 70% for Trafilatura
- **Maintenance:** Zero maintenance (no browser automation, no parser updates)
- **Output quality:** Clean markdown with minimal boilerplate

**Trade-off:** Cost ($20-50/month) and external dependency, but developer time saved is 10x the cost.

**Fallback:** Trafilatura used when Firecrawl fails or is disabled.

See: [Scraper chain explainer](scraper-chain.md)

---

### Why OpenRouter Over Direct LLM APIs?

**Chosen:** OpenRouter (LLM aggregator) **Rejected:** Direct OpenAI API, Anthropic API, Gemini API

**Rationale:**

- **Unified interface:** One API for 100+ models (OpenAI, Anthropic, Google, DeepSeek, Qwen)
- **Fallback chains:** Automatic model switching on rate limits or outages
- **Cost optimization:** Switch to cheaper models without code changes
- **Free tier:** Multiple free models (Gemini 2.0 Flash, DeepSeek R1)

**Trade-off:** Extra network hop (adds ~50-100ms latency), but flexibility is worth it.

---

### Why Telethon Over python-telegram-bot?

**Chosen:** Telethon (MTProto async client) **Rejected:** python-telegram-bot, aiogram

**Rationale:**

- **Async-first:** Native asyncio support (vs bolt-on async in python-telegram-bot)
- **MTProto:** Direct Telegram protocol (no Bot API HTTP bottleneck)
- **Performance:** 30-40% faster message handling
- **Type hints:** Better IDE support and type checking

**Trade-off:** Slightly more complex setup (requires `API_ID` and `API_HASH`), but performance gain is significant.

---

## Design Patterns

### Repository Pattern (Persistence Abstraction)

**Location:** `app/infrastructure/persistence/`

**Purpose:** Isolate database access from business logic.

**Example:**

```python
class SummaryRepository:
    async def save(self, summary: Summary) -> None:
        # SQLAlchemy 2.0 async session logic here
        ...

    async def find_by_id(self, summary_id: int) -> Optional[Summary]:
        # Query logic here
        ...
```

**Benefits:** Swap SQLite for PostgreSQL by changing one class, no domain code changes.

---

### Factory Pattern (Object Creation)

**Location:** `app/di/` (split across `api.py`, `application.py`, `telegram.py`, `repositories.py`, `shared.py`, etc.)

**Purpose:** Centralize object creation and dependency injection.

**Example:**

```python
def create_url_processor() -> URLProcessor:
    return build_url_processor(
        cfg=cfg,
        db=db,
        firecrawl=scraper_chain,
        llm_client=llm_client,
        response_formatter=response_formatter,
        audit_func=audit_sink,
        sem=semaphore_factory,
        request_repo=build_request_repository(db),
        summary_repo=build_summary_repository(db),
    )
```

**Benefits:** Dependencies are explicit, testing is easy (inject mocks).

---

### Strategy Pattern (Pluggable Algorithms)

**Location:** `app/adapters/llm/protocol.py`, `app/adapters/llm/factory.py`, `app/adapters/openrouter/`, `app/adapters/llm/openai/`, `app/adapters/llm/anthropic/`

**Purpose:** Swap LLM providers without changing caller code.

**Example:**

```python
class LLMClientProtocol(Protocol):
    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        stream: bool = False,
        on_stream_delta: Callable[[str], Awaitable[None] | None] | None = None,
        per_model_timeout_sec: float | None = None,
        per_model_timeout_overrides: dict[str, float] | None = None,
    ) -> LLMCallResult: ...

class OpenRouterClient(LLMClientProtocol): ...
class OpenAIClient(LLMClientProtocol): ...
class AnthropicClient(LLMClientProtocol): ...
```

**Benefits:** Add new LLM providers by implementing the real workflow protocol instead of accepting arbitrary kwargs in the generic summarization path.

---

### Chain of Responsibility (Error Handling)

**Location:** `app/adapters/openrouter/error_handler.py`

**Purpose:** Handle different error types with specific recovery strategies.

**Example:**

```python
if response.status == 429:  # Rate limit
    await asyncio.sleep(retry_after)
elif response.status == 503:  # Service unavailable
    switch_to_fallback_model()
elif response.status == 400:  # Invalid request
    raise_user_error()
```

**Benefits:** Clear separation of error handling logic.

---

## Testing Philosophy

### Unit Tests: Test Behavior, Not Implementation

**Good:**

```python
def test_url_normalization_removes_tracking_params():
    assert normalize_url("https://example.com?utm_source=x") == "https://example.com"
```

**Bad:**

```python
def test_url_normalization_calls_urlparse():
    with mock.patch("urllib.parse.urlparse") as m:
        normalize_url("https://example.com")
        assert m.called
```

**Rationale:** Testing implementation details makes refactoring painful. Test observable behavior instead.

---

### Integration Tests: Mock External Services, Not Internal Code

**Pattern:** Use `httpx.mock` to stub Firecrawl/OpenRouter responses, test end-to-end flow.

**Benefits:** Confidence that components integrate correctly without hitting real APIs.

---

### E2E Tests: Gated by Environment Variable

**Pattern:** `E2E=1 pytest tests/e2e/` runs against real Firecrawl/OpenRouter APIs.

**Rationale:** E2E tests are slow and cost money (API calls), run only in CI or on-demand.

---

## Performance Considerations

### Async Everything

**Rationale:** Summarization is I/O-bound (waiting on Firecrawl and OpenRouter). Async allows processing 10+ requests concurrently on a single thread.

---

### Semaphore-Based Rate Limiting

**Pattern:** `asyncio.Semaphore(MAX_CONCURRENT_CALLS)` limits concurrent API calls.

**Rationale:** Prevents rate limiting from Firecrawl/OpenRouter, respects free tier quotas.

---

### Token Counting Approximation

**Pattern:** `len(text) // 4` instead of tiktoken for most cases.

**Rationale:** Tiktoken is accurate but slow (100ms for long texts). Approximation is 10x faster and accurate within 10%.

**Trade-off:** May send slightly more tokens than allowed, but cost impact is negligible.

---

## Security Considerations

### Principle of Least Privilege

**Applied:**

- Bot token has no special permissions (only send/receive messages)
- SQLite database has no network access
- Docker container runs as non-root user
- Environment variables never logged (even in DEBUG mode)

---

### Input Validation

**Pattern:** Validate all user input before processing.

**Example:**

```python
if not is_valid_url(url):
    raise ValueError(f"Invalid URL: {url}")
```

**Rationale:** Prevent injection attacks, malformed data from crashing the bot.

---

### Secrets Management

**Pattern:** All secrets via environment variables, never hardcoded.

**Enforcement:** Pre-commit hooks scan for leaked secrets (gitleaks).

---

## Documentation Philosophy

### Diátaxis Framework

**Structure:** Tutorials, How-To Guides, Reference, Explanation.

**Rationale:** Different users need different documentation types. Mixing them makes everything harder to find.

See: [Documentation Hub](../README.md)

---

### Code Comments: Explain Why, Not What

**Good:**

```python
# Firecrawl returns markdown with triple backticks, strip them
content = content.strip("```")
```

**Bad:**

```python
# Strip triple backticks from content
content = content.strip("```")
```

**Rationale:** Code already shows what it does. Comments should explain non-obvious decisions.

---

## Evolution Strategy

### When to Refactor

**Trigger:** Third instance of similar code.

**Rationale:** Two instances = coincidence, three instances = pattern. Extract into function/class on third occurrence.

---

### When to Add Complexity

**Trigger:** User pain point measured in hours, not minutes.

**Example:** Redis caching added when users reported high API costs.

**Rationale:** Don't optimize for hypothetical problems. Wait for real user pain.

---

### When to Remove Features

**Trigger:** Feature unused by any user for 6+ months.

**Example:** (None yet, all features actively used)

**Rationale:** Every feature has maintenance cost. Remove dead code ruthlessly.

---

## See Also

- [Summary Contract Design](summary-contract-design.md) - Why strict JSON schema
- [Observability Strategy](observability-strategy.md) - Logging and debugging approach
- [Hexagonal Architecture](architecture-overview.md#layering-quick-reference) - Ports and Adapters pattern
- [Multi-Agent Architecture](multi-agent-architecture.md) - LLM pipeline design

---

**Last Updated:** 2026-02-09
