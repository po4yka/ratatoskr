# Summary JSON Contract

Complete specification for the strict JSON schema enforced by Ratatoskr for all summaries.

**Audience:** Developers, Integrators **Type:** Reference **Related:** [Summary Contract Design](../explanation/summary-contract-design.md), [SPEC.md § Summary JSON Contract](../SPEC.md#summary-json-contract-canonical)

---

## Contract Version

**Current Version:** 3.0 **Last Updated:** 2026-05-23

**Validation Location:** `app/core/summary_contract.py` **Pydantic Model:** `app/core/summary_schema.py` **Runtime Descriptor:** `SummaryContractDescriptor(default)`

---

## Runtime Binding

The default summary contract is registered through `DEFAULT_SUMMARY_CONTRACT_DESCRIPTOR` in `app/core/summary_contract.py`. The descriptor is the single runtime bundle for the current contract shape:

- `contract_id="default"` and `schema_name="summary_schema"`
- `supported_languages=("en", "ru")`
- `schema_loader=get_summary_json_schema`
- `prompt_loader=PromptManager.get_system_prompt` through the contract prompt wrapper
- `compatibility_mapper=validate_and_shape_summary`
- `response_format("json_schema")` returns a strict provider-native JSON Schema response format; the generic and repair paths still use JSON object response formats where provider support requires it

Workflow code should fetch the descriptor with `get_summary_contract_descriptor()` instead of manually pairing prompt files, schema names, and validation functions. This keeps the existing 3.0 payload backward-compatible while making future summary variants explicit rather than hidden behind ad hoc kwargs or duplicated schema literals.

---

## JSON Schema

### Core Summaries

```json
{
  "summary_250": "Ultra-brief summary (150-250 characters)",
  "summary_1000": "Comprehensive summary (800-1200 characters)",
  "tldr": "One-sentence key takeaway (50-100 characters)"
}
```

**Validation:**

- `summary_250`: Exactly 150-250 characters, sentence boundary
- `summary_1000`: Exactly 800-1200 characters, multi-sentence
- `tldr`: 50-100 characters, one sentence

---

### Analysis Fields

```json
{
  "key_ideas": ["idea 1", "idea 2", "idea 3"],
  "topic_tags": ["#tag1", "#tag2", "#tag3"],
  "entities": {
    "people": ["Person Name"],
    "organizations": ["Org Name"],
    "locations": ["Location"],
    "dates": ["2026-02-09"],
    "technologies": ["Technology"]
  },
  "estimated_reading_time_min": 7,
  "readability": {
    "flesch_kincaid_grade_level": 12.4,
    "audience_level": "College",
    "reading_ease_score": 45.2
  },
  "key_stats": [
    {
      "value": "12.3 billion",
      "context": "Market size in 2026",
      "significance": "Represents 25% growth",
      "source_excerpt": "According to the report..."
    }
  ]
}
```

**Validation:**

- `key_ideas`: 3-8 strings, deduplicated, 3-10 words each
- `topic_tags`: 3-8 strings, leading `#`, deduplicated, max 10
- `entities.*`: Deduplicated case-insensitively
- `entities` object-array inputs ignore metadata-only records unless an actual entity value is present
- `estimated_reading_time_min`: Integer >= 0
- `readability.audience_level`: Enum (Elementary, Middle School, High School, College, Graduate)

---

### Quality Indicators

```json
{
  "confidence": {
    "overall_confidence": 0.85,
    "confidence_reasoning": "High-quality source, clear content structure",
    "per_field_confidence": {
      "summary": 0.9,
      "key_ideas": 0.85,
      "entities": 0.7
    }
  },
  "hallucination_risk": {
    "level": "low",
    "reasoning": "Content directly extracted from source",
    "mitigation": "Cross-referenced with metadata"
  },
  "quality_scores": {
    "accuracy": 0.9,
    "completeness": 0.85,
    "coherence": 0.95,
    "relevance": 0.88
  }
}
```

**Validation:**

- All confidence scores: 0.0-1.0 range
- `hallucination_risk.level`: Enum (low, medium, high)
- `quality_scores.*`: 0.0-1.0 range

---

### Search and Discovery

```json
{
  "seo_keywords": ["keyword1", "keyword2", "keyword3"],
  "query_expansion_keywords": ["alternative term 1", "synonym 1"],
  "semantic_boosters": ["contextual sentence 1", "related concept 1"],
  "topic_taxonomy": {
    "primary_category": "technology",
    "subcategories": ["artificial intelligence", "machine learning"],
    "industry_tags": ["software", "research"]
  }
}
```

**Validation:**

- `seo_keywords`: 5-10 strings
- `query_expansion_keywords`: 5-10 strings
- `semantic_boosters`: 5-10 strings
- `topic_taxonomy.primary_category`: Required string

---

### Content Metadata

```json
{
  "source_type": "article",
  "content_type": "technical",
  "temporal_freshness": {
    "is_time_sensitive": false,
    "temporal_indicators": ["published 2026-02-09", "annual report"],
    "freshness_decay_rate": "medium"
  },
  "extractive_quotes": [
    {
      "quote": "Direct quote from source",
      "context": "Why this quote matters",
      "significance": "Importance rating",
      "position_in_text": 0.25
    }
  ],
  "answered_questions": ["What is X?", "How does Y work?"]
}
```

**Validation:**

- `source_type`: Enum (article, video, academic_paper, podcast, documentation, blog_post, news_article, social_media, forum_post, other)
- `content_type`: Enum (technical, general, academic, opinion, news, tutorial, review)
- `extractive_quotes`: 0-5 quotes
- `questions_answered` textual forms (`Q:/A:` and `Question:/Answer:`) are parsed with Unicode-safe boundary handling

---

### Semantic Chunking

```json
{
  "semantic_chunks": [
    {
      "article_id": "sha256_hash_of_url",
      "section": "Introduction",
      "language": "en",
      "topics": ["machine learning", "AI"],
      "text": "100-200 word chunk of content...",
      "local_summary": "1-2 sentence summary of this chunk",
      "local_keywords": ["keyword1", "keyword2", "keyword3"]
    }
  ]
}
```

**Validation:**

- `semantic_chunks`: Array of chunk objects
- `text`: 100-200 words per chunk
- `local_keywords`: 3-8 phrases

---

### Additional Insights

```json
{
  "insights": [
    {
      "type": "trend",
      "insight": "Describes emerging pattern or trend",
      "supporting_evidence": ["fact 1", "fact 2"],
      "confidence": 0.8
    }
  ],
  "related_topics": ["related topic 1", "related topic 2"],
  "prerequisites": ["prerequisite concept 1"],
  "follow_up_resources": [
    {
      "title": "Resource title",
      "url": "https://example.com",
      "description": "Why this resource is relevant"
    }
  ]
}
```

**Validation:**

- `insights[].type`: Enum (trend, implication, contradiction, gap, connection)
- `insights[].confidence`: 0.0-1.0 range

---

## Complete Example

```json
{
  "summary_250": "A breakthrough in quantum computing achieved by researchers at MIT demonstrates stable qubits at room temperature, potentially revolutionizing the field by eliminating expensive cooling requirements.",
  "summary_1000": "Researchers at MIT have announced a significant breakthrough in quantum computing technology by successfully demonstrating stable quantum bits (qubits) operating at room temperature. This achievement addresses one of the field's major challenges: the need for extremely low temperatures to maintain quantum coherence. The team's novel approach uses topological protection mechanisms and error correction codes that work synergistically to preserve quantum states without cryogenic cooling. Early benchmarks show comparable performance to traditional superconducting qubits while dramatically reducing operational costs. Industry experts suggest this could accelerate quantum computer adoption in commercial settings, particularly for optimization problems and cryptography applications. The research has been peer-reviewed and published in Nature Physics, with the team planning to scale up their prototype from 8 qubits to 64 within the next year.",
  "tldr": "MIT achieves room-temperature quantum computing, eliminating expensive cooling needs.",
  "key_ideas": [
    "Room-temperature qubits eliminate cooling costs",
    "Topological protection maintains quantum coherence",
    "Comparable performance to superconducting qubits",
    "Accelerates commercial quantum computer adoption",
    "Scaling from 8 to 64 qubits planned"
  ],
  "topic_tags": ["#quantumcomputing", "#MIT", "#breakthrough", "#technology", "#physics"],
  "entities": {
    "people": [],
    "organizations": ["MIT", "Nature Physics"],
    "locations": ["Cambridge"],
    "dates": ["2026"],
    "technologies": ["quantum computing", "qubits", "topological protection", "error correction"]
  },
  "estimated_reading_time_min": 5,
  "key_stats": [
    {
      "value": "8 qubits",
      "context": "Current prototype size",
      "significance": "Foundation for scaling to 64 qubits",
      "source_excerpt": "prototype from 8 qubits to 64 within the next year"
    }
  ],
  "answered_questions": [
    "What breakthrough did MIT achieve?",
    "How does it work?",
    "What are the commercial implications?"
  ],
  "readability": {
    "flesch_kincaid_grade_level": 14.2,
    "audience_level": "College",
    "reading_ease_score": 42.1
  },
  "seo_keywords": [
    "quantum computing breakthrough",
    "room temperature qubits",
    "MIT quantum research",
    "topological protection",
    "quantum error correction"
  ],
  "source_type": "article",
  "content_type": "technical",
  "temporal_freshness": {
    "is_time_sensitive": true,
    "temporal_indicators": ["2026", "within the next year"],
    "freshness_decay_rate": "high"
  },
  "extractive_quotes": [
    {
      "quote": "stable quantum bits operating at room temperature",
      "context": "Core achievement of the research",
      "significance": "Solves major industry challenge",
      "position_in_text": 0.15
    }
  ],
  "confidence": {
    "overall_confidence": 0.92,
    "confidence_reasoning": "Peer-reviewed research from reputable institution",
    "per_field_confidence": {
      "summary": 0.95,
      "key_ideas": 0.92,
      "entities": 0.88
    }
  },
  "hallucination_risk": {
    "level": "low",
    "reasoning": "Content directly from published research",
    "mitigation": "Cross-referenced with Nature Physics publication"
  },
  "quality_scores": {
    "accuracy": 0.93,
    "completeness": 0.88,
    "coherence": 0.96,
    "relevance": 0.91
  },
  "topic_taxonomy": {
    "primary_category": "technology",
    "subcategories": ["quantum computing", "physics", "research"],
    "industry_tags": ["hardware", "scientific research"]
  },
  "query_expansion_keywords": [
    "quantum bits",
    "qubit stability",
    "room temp quantum",
    "quantum coherence",
    "cryogenic cooling alternatives"
  ],
  "semantic_boosters": [
    "Quantum computing advances enable new cryptography methods",
    "Topological qubits resist environmental noise",
    "Commercial quantum applications in optimization"
  ],
  "semantic_chunks": [
    {
      "article_id": "abc123...",
      "section": "Introduction",
      "language": "en",
      "topics": ["quantum computing", "breakthrough"],
      "text": "Researchers at MIT have announced a significant breakthrough...",
      "local_summary": "MIT achieves room-temperature quantum computing.",
      "local_keywords": ["MIT", "quantum computing", "room temperature"]
    }
  ],
  "insights": [
    {
      "type": "trend",
      "insight": "Room-temperature quantum computing could democratize access by reducing infrastructure costs",
      "supporting_evidence": [
        "Eliminates expensive cryogenic cooling",
        "Comparable performance to traditional systems"
      ],
      "confidence": 0.85
    }
  ],
  "related_topics": ["superconducting qubits", "quantum error correction", "topological computing"],
  "prerequisites": ["basic quantum mechanics", "computer science fundamentals"],
  "follow_up_resources": [
    {
      "title": "Nature Physics Publication",
      "url": "https://nature.com/...",
      "description": "Original peer-reviewed research paper"
    }
  ]
}
```

---

## Validation Rules

### Character Limits

| Field | Min | Max | Notes |
| ------- | ----- | ----- | ------- |
| `summary_250` | 150 | 250 | Sentence boundary |
| `summary_1000` | 800 | 1200 | Multi-sentence |
| `tldr` | 50 | 100 | One sentence |
| `key_ideas[]` | 10 | 150 | Per idea, 3-10 words recommended |
| `topic_tags[]` | 3 | 30 | Per tag, including `#` |

### Array Lengths

| Field | Min Items | Max Items |
| ------- | ----------- | ----------- |
| `key_ideas` | 3 | 8 |
| `topic_tags` | 3 | 10 |
| `entities.people` | 0 | unlimited |
| `entities.organizations` | 0 | unlimited |
| `entities.locations` | 0 | unlimited |
| `seo_keywords` | 5 | 10 |
| `query_expansion_keywords` | 5 | 10 |
| `semantic_boosters` | 5 | 10 |
| `extractive_quotes` | 0 | 5 |
| `semantic_chunks` | 0 | unlimited |
| `insights` | 0 | 10 |

### Enums

**source_type:**

- `article`
- `video`
- `academic_paper`
- `podcast`
- `documentation`
- `blog_post`
- `news_article`
- `social_media`
- `forum_post`
- `other`

**content_type:**

- `technical`
- `general`
- `academic`
- `opinion`
- `news`
- `tutorial`
- `review`

**readability.audience_level:**

- `Elementary` (grades 1-5)
- `Middle School` (grades 6-8)
- `High School` (grades 9-12)
- `College` (undergraduate)
- `Graduate` (postgraduate)

**hallucination_risk.level:**

- `low`
- `medium`
- `high`

**insights[].type:**

- `trend`
- `implication`
- `contradiction`
- `gap`
- `connection`

**temporal_freshness.freshness_decay_rate:**

- `low` (evergreen content)
- `medium` (relevant for months/years)
- `high` (time-sensitive, relevant for days/weeks)

---

## Validation Functions

### Python (app/core/summary_contract.py)

```python
descriptor = get_summary_contract_descriptor("default")
response_format = descriptor.response_format("json_schema")
system_prompt = descriptor.prompt_loader(lang="en")

def validate_and_shape_summary(summary: dict) -> dict:
    """Validate and backfill summary JSON against contract."""
    # Enforce character limits
    validate_field_char_limits(summary)

    # Deduplicate arrays
    deduplicate_arrays(summary)

    # Validate confidence scores (0.0-1.0)
    validate_confidence_scores(summary)

    # Backfill optional fields with defaults
    backfill_optional_fields(summary)

    return summary
```

### Self-Correction Loop

If validation fails, the LLM is retried with error feedback (up to 3 attempts):

```python
try:
    validated_summary = validate_and_shape_summary(llm_response)
except ValidationError as e:
    retry_with_error_feedback(
        prompt=original_prompt,
        error=str(e),
        attempt=attempt_number
    )
```

**Success Rate:** 94%+ with self-correction.

---

## Backward Compatibility

**Strategy:** Never remove fields, only add optional fields.

**Reason:** Existing database summaries must remain valid.

**Approach:**

- New fields are optional with defaults
- Validation backfills missing fields for old summaries
- Pydantic model uses `Optional[...]` for new fields

**Example Migration:**

```python
# Version 2.0 adds quality_scores (optional)
summary_v2 = {
    **summary_v1,
    "quality_scores": {
        "accuracy": 0.8,
        "completeness": 0.75,
        "coherence": 0.85,
        "relevance": 0.9
    }
}
```

---

## See Also

- [Summary Contract Design](../explanation/summary-contract-design.md) - Design rationale
- [SPEC.md § Summary JSON Contract](../SPEC.md#summary-json-contract-canonical) - Canonical specification

---

**Last Updated:** 2026-05-23
