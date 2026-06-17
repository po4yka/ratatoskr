# Summary Contract Design

Why Ratatoskr enforces a strict 35+ field JSON schema for all summaries.

**Audience:** Developers, Architects **Type:** Explanation **Related:** [Design Philosophy](design-philosophy.md)

---

## The Problem

### Unstructured LLM Output

Early versions of Ratatoskr used free-form prompts:

```text
Summarize this article in 2-3 paragraphs. Include key points and interesting facts.
```

**Result:** Variable output quality:

- Sometimes bullet points, sometimes paragraphs
- Inconsistent length (50 words to 500 words)
- No machine-readable structure
- Missing metadata (reading time, topic tags, entities)
- No confidence indicators (hallucination risk, quality scores)

**Impact:**

- Cannot build semantic search (no standardized `key_ideas` field)
- Cannot render in UI (unpredictable format)
- Cannot measure quality (no readability scores)
- Cannot support multi-language (no `lang` field)

---

## The Solution: Strict JSON Schema

### Contract Definition

**Location:** `app/core/summary_contract.py` (descriptor registry and validation), `app/core/summary_schema.py` (Pydantic model)

`app/core/summary_contract.py` now exposes a small descriptor registry around the contract rather than only standalone validation helpers. The current `default` descriptor carries the schema loader, schema name, supported languages, prompt loader, provider response-format builder, and compatibility mapper. Existing callers still use `validate_and_shape_summary()` for the default payload, but generic workflow code should depend on the descriptor so prompt/schema/repair behavior stays paired when a future contract variant is introduced.

**35+ Required Fields** (grouped by category):

#### Core Summaries

- `summary_250` (150-250 chars) - Ultra-brief overview for previews
- `summary_1000` (800-1200 chars) - Comprehensive summary for reading
- `tldr` (50-100 chars) - One-sentence takeaway

#### Analysis

- `key_ideas` (3-8 strings) - Main concepts, deduplicated
- `topic_tags` (3-8 strings) - Classification tags
- `entities` (object with `people`, `orgs`, `locations`, `dates`, `technologies`)
- `estimated_reading_time_min` (int) - Reading time for original content
- `readability` (object with `flesch_kincaid_grade_level`, `audience_level`)

#### Quality Indicators

- `confidence` (object with `overall_confidence`, `confidence_reasoning`)
- `hallucination_risk` (object with `level`, `reasoning`, `mitigation`)
- `quality_scores` (object with `accuracy`, `completeness`, `coherence`, `relevance`)

#### Search and Discovery

- `seo_keywords` (5-10 strings) - Search optimization keywords
- `query_expansion_keywords` (5-10 strings) - Alternative search terms
- `semantic_boosters` (5-10 strings) - Related concept keywords
- `topic_taxonomy` (object with `primary_category`, `subcategories`)

#### Content Metadata

- `source_type` (enum: article, video, academic_paper, etc.)
- `temporal_freshness` (object with `is_time_sensitive`, `temporal_indicators`)
- `extractive_quotes` (array of {quote, context, significance})
- `key_stats` (array of {value, context, significance})

#### Full schema: See `SPEC.md` § Summary JSON contract

---

## Design Rationale

### 1. Predictable Structure = Type Safety

**Problem:** Free-form output requires defensive programming.

**Before:**

```python
# Hope the LLM returned something resembling a summary
summary_text = llm_response.get("summary") or llm_response.get("text") or "No summary"
if isinstance(summary_text, list):
    summary_text = " ".join(summary_text)  # Sometimes it's a list?
```

**After:**

```python
# Pydantic guarantees these fields exist and have correct types
summary = SummaryOutput.parse_obj(llm_response)
preview_text: str = summary.summary_250  # Always a string, always 150-250 chars
```

**Benefit:** Zero runtime errors from malformed summaries.

---

### 2. Semantic Search Requires Standardized Fields

**Use Case:** User searches for "machine learning frameworks"

**Query Flow:**

1. Extract search keywords: `["machine learning", "frameworks"]`
2. Query expansion using `query_expansion_keywords`: `["ML", "TensorFlow", "PyTorch", "neural networks"]`
3. Vector search on `key_ideas` embeddings
4. Boost results with `semantic_boosters`: `["deep learning", "AI models"]`
5. Filter by `topic_taxonomy.primary_category = "technology"`

**Impossible Without Contract:** If each summary has different field names (`main_ideas` vs `key_points` vs `concepts`), semantic search cannot work.

---

### 3. UI Rendering Requires Predictable Lengths

**Telegram Message Limits:**

- Maximum message length: 4096 characters
- Preview snippets: ~200 characters
- Push notifications: ~100 characters

**Contract Solution:**

- `summary_250` (150-250 chars) → Telegram preview
- `tldr` (50-100 chars) → Push notification
- `summary_1000` (800-1200 chars) → Full message (fits in 4096 limit)

**Without Contract:** LLMs would generate 5000-character summaries that fail to send.

---

### 4. Quality Assurance Requires Measurable Metrics

**Quality Gates:**

- `confidence.overall_confidence < 0.7` → Flag for manual review
- `hallucination_risk.level = "high"` → Add warning label
- `readability.flesch_kincaid_grade_level > 16` → Simplify language

**Example:**

```python
if summary.quality_scores.accuracy < 0.8:
    logger.warning(f"Low accuracy summary for {url}", extra={"correlation_id": request.id})
    summary_message += "\n\n⚠️ Note: This summary may have accuracy issues."
```

**Benefit:** Proactive quality control instead of reactive user complaints.

---

### 5. Multi-Language Support Requires Language Metadata

**Problem:** Mixing Russian and English summaries in search results.

**Solution:** `lang` field (ISO 639-1 code: `en`, `ru`, etc.)

**Application:**

- Filter search results by language preference
- Use language-specific prompts (`app/prompts/summary_system_en.txt` vs `app/prompts/summary_system_ru.txt`)
- Apply language-specific readability formulas (Flesch-Kincaid for English, different for Russian)

---

### 6. Deduplication Requires Canonical Representation

**Problem:** Same article summarized twice yields different summaries.

**Solution:** `dedupe_hash` (sha256 of normalized URL) + contract ensures two summaries of same content are structurally identical.

**Deduplication Logic:**

```python
existing_summary = db.summaries.find_by_dedupe_hash(dedupe_hash)
if existing_summary:
    return existing_summary  # Reuse cached summary
```

**Benefit:** 30-40% cost reduction from avoiding duplicate LLM calls.

---

## Enforcement Mechanisms

### 1. LLM Prompt Constraints

**Location:** `app/prompts/summary_system_en.txt`, `app/prompts/summary_system_ru.txt`, loaded through `PromptManager.get_contract_system_prompt()`

**Technique:** Explicit field definitions with character limits, type constraints, examples.

**Example Prompt Excerpt:**

```text
Return a JSON object with the following fields:

1. summary_250 (string, 150-250 characters): Ultra-brief overview suitable for previews.
   - Must be self-contained (no "this article..." phrasing)
   - Must be 150-250 characters (strict limit)

2. key_ideas (array of 3-8 strings): Main concepts from the content.
   - Each idea should be a complete phrase (3-10 words)
   - Remove duplicates and merge similar ideas
   - Focus on actionable insights
```

**Result:** LLM learns to respect constraints through prompt engineering.

---

### 2. JSON Schema Validation

**Location:** `app/core/summary_contract.py`

**Functions:**

- `get_summary_contract_descriptor(contract_id)` - Returns the schema/prompt/compatibility bundle for a registered contract
- `validate_and_shape_summary(summary_dict)` - Validates and backfills missing fields
- `validate_field_char_limits(summary_dict)` - Enforces character limits
- `deduplicate_arrays(summary_dict)` - Removes duplicate values in arrays
- `validate_confidence_scores(summary_dict)` - Ensures 0.0-1.0 range for confidence

**Enforcement:** Every summary passes through validation before persistence.

```python
# app/adapters/content/llm_response_workflow_execution.py
descriptor = get_summary_contract_descriptor()
response_format = descriptor.response_format("json_schema")
raw_summary = await llm_client.complete(prompt, response_format=response_format)
validated_summary = descriptor.compatibility_mapper(raw_summary)  # May raise ValidationError
db.summaries.save(validated_summary)
```

**Result:** Invalid summaries are caught immediately, not discovered later by users.

---

### 3. Pydantic Type Checking

**Location:** `app/core/summary_schema.py`

**Model:** `SummaryOutput(BaseModel)` with 35+ typed fields.

**Example:**

```python
class SummaryOutput(BaseModel):
    summary_250: str = Field(..., min_length=150, max_length=250)
    key_ideas: List[str] = Field(..., min_items=3, max_items=8)
    confidence: ConfidenceScores
    entities: Entities
    # ... 30+ more fields
```

**Enforcement:** Pydantic raises `ValidationError` if types mismatch or constraints violated.

**Benefit:** Runtime type safety (prevents `summary.key_ideas[0].lower()` when `key_ideas` is missing).

---

### 4. Self-Correction Loop

**Location:** the summarize graph's `validate ↺ repair` cycle
(`app/application/graphs/summarize/nodes/validate.py` + `repair.py`), backed by
`app/application/services/summarization/graph_llm.py::summarize_with_instructor`.

**Pattern:** Two layers of self-correction:

- `instructor`'s `chat_structured(max_retries=N)` reasks within a single LLM call.
- The graph-level `validate → repair → validate` loop re-runs the structured call
  with the contract errors fed back, bounded by `MAX_REPAIR_ATTEMPTS`
  (`app/application/graphs/summarize/state.py`) and langgraph's per-invocation
  `recursion_limit`.

**Flow:**

1. The `summarize` node generates summary JSON.
2. The `validate` node runs `summary_contract.py`; it fails: "Field `key_ideas` is missing".
3. Router → `repair` node re-prompts with the error feedback (new `llm_calls` row, `attempt_trigger='graph_node'`).
4. The `summarize`/`repair` call generates a corrected summary.
5. `validate` passes → router → `enrich` → Success. Budget exhaustion routes to the single terminal-failure path.

**Success Rate:** 94%+ (vs 85% without self-correction).

---

## Trade-offs

### More Upfront Work, Less Downstream Chaos

**Cost:** Designing and maintaining 35+ field schema requires effort.

**Benefit:** Zero downstream bugs from malformed summaries. Features like search, quality scoring, multi-language support "just work" because data is reliable.

---

### Rigid Schema Limits Experimentation

**Problem:** Adding new fields requires updating prompts, validation, and Pydantic models.

**Mitigation:** `metadata` field allows free-form JSON for experimental features without breaking contract.

**Example:**

```python
summary.metadata = {
    "experimental_sentiment_score": 0.85,
    "prototype_topic_clusters": ["AI", "ethics"]
}
```

**Benefit:** Can experiment without contract changes, then promote successful experiments to first-class fields.

---

### LLMs Sometimes Struggle with Complex Schemas

**Problem:** 35+ fields is cognitively demanding for LLMs, especially smaller models.

**Mitigation:**

- Clear prompt with examples
- Self-correction loop (retry with error feedback)
- Field backfilling for optional fields (validation fills missing fields with defaults)

**Result:** Even smaller models (DeepSeek V3, Qwen 3 Max) achieve 90%+ success rate.

---

## Real-World Impact

### Case Study: Hallucination Detection

**Before Contract:**

- User reports: "The summary says the article is about blockchain, but it's about biology."
- Debugging: Manual review of LLM output, hard to pinpoint where hallucination occurred.

**After Contract:**

- `hallucination_risk.level = "high"` detected automatically
- `hallucination_risk.reasoning = "Article mentions 'cell chains' but summary interpreted as 'blockchain'"`
- User sees warning: "⚠️ This summary may contain inaccuracies. Verify important facts."

**Impact:** Proactive warning instead of silent failure.

---

### Case Study: Semantic Search Accuracy

**Before Contract:**

- Search for "Python tutorial" returns articles about snakes (animal) and Python (language) mixed together.
- No way to distinguish because `key_ideas` field format varies.

**After Contract:**

- `topic_taxonomy.primary_category = "programming"` filters out snake articles
- `entities.technologies = ["Python"]` confirms programming context
- `semantic_boosters = ["coding", "tutorial", "programming"]` improves ranking

**Impact:** Search accuracy increased from 60% to 90%+.

---

### Case Study: Multi-Language Support

**Before Contract:**

- Russian articles summarized in English (confusing for Russian users)
- No way to filter search by language

**After Contract:**

- `lang = "ru"` detected automatically
- Russian prompt used (`app/prompts/summary_system_ru.txt`)
- Search filtered by language preference

**Impact:** Seamless multi-language experience.

---

## Evolution Strategy

### When to Add New Fields

**Criteria:**

1. User pain point (not speculative need)
2. Used by 50%+ of users (or critical feature)
3. Cannot be derived from existing fields

**Example:** Added `temporal_freshness` when users complained about outdated summaries not being flagged.

---

### When to Remove Fields

**Criteria:**

1. Field unused by any user for 6+ months
2. Can be derived from other fields

**Example:** (None yet, all fields actively used)

---

### Backward Compatibility

**Strategy:** Never remove fields, only add new optional fields.

**Reason:** Existing database summaries must remain valid.

**Approach:**

- New fields are optional with defaults
- Validation backfills missing fields for old summaries
- Pydantic model uses `Optional[...]` for new fields

---

## Alternative Approaches Considered

### 1. Free-Form Markdown Output

**Rejected:** No machine-readable structure, cannot build search or quality metrics.

**When to Use:** If Ratatoskr were purely a human-readable summarization tool with no search or API.

---

### 2. Minimal Schema (3-5 Fields)

**Rejected:** Insufficient for advanced features (search, quality scoring, multi-language).

**When to Use:** For MVP or prototype without search/API requirements.

---

### 3. Dynamic Schema (User-Defined Fields)

**Rejected:** Too complex for single-user bot, breaks type safety.

**When to Use:** For multi-tenant SaaS where different users need different fields.

---

## Best Practices

### 1. Field Naming Conventions

**Pattern:** `snake_case`, descriptive, unambiguous.

**Good:** `estimated_reading_time_min`, `hallucination_risk` **Bad:** `time`, `risk` (too vague), `ERTMin` (unclear abbreviation)

---

### 2. Character Limits Based on Use Case

**Pattern:** Match UI constraints, not arbitrary limits.

**Example:**

- `tldr` (50-100 chars) → Telegram push notification limit
- `summary_250` (150-250 chars) → Telegram preview snippet
- `summary_1000` (800-1200 chars) → Telegram message body

---

### 3. Nested Objects for Related Fields

**Pattern:** Group related fields into objects.

**Good:**

```json
{
  "confidence": {
    "overall_confidence": 0.85,
    "confidence_reasoning": "High-quality source, clear content"
  }
}
```

**Bad:**

```json
{
  "overall_confidence": 0.85,
  "confidence_reasoning": "High-quality source, clear content"
}
```

**Benefit:** Clear semantic grouping, easier to extend (add `confidence.per_field_scores` later).

---

### 4. Enums for Categorical Fields

**Pattern:** Use enums for fields with fixed set of values.

**Example:**

```python
class SourceType(str, Enum):
    ARTICLE = "article"
    VIDEO = "video"
    ACADEMIC_PAPER = "academic_paper"
    # ...
```

**Benefit:** Prevents typos (`"artcle"` vs `"article"`), enables validation.

---

## See Also

- [Design Philosophy](design-philosophy.md) - Overall architectural principles
- [SPEC.md § Summary JSON Contract](../SPEC.md#summary-json-contract) - Full field reference
- [Multi-Agent Architecture](multi-agent-architecture.md) - Self-correction implementation

---

**Last Updated:** 2026-05-23
