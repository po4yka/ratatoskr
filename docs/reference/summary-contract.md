# Summary JSON Contract

Ratatoskr persists one canonical summary shape. The executable sources of truth
are:

- `app/core/summary_schema.py` — Pydantic models and enums;
- `app/core/summary_contract.py` — the runtime descriptor and public helpers;
- `app/core/summary_contract_impl/` — normalization, shaping, and provider schema;
- `app/prompts/summary_system_en.txt` and `summary_system_ru.txt` — paired model
  instructions.

There is no independent numeric summary-contract version in code. The registered
contract ID is `default`; the prompt/cache version is separately configured by
`SUMMARY_PROMPT_VERSION`.

## Runtime descriptor

`get_summary_contract_descriptor("default")` returns a single bundle:

| Property | Current value |
| --- | --- |
| `contract_id` | `default` |
| `schema_name` | `summary_schema` |
| supported prompt languages | `en`, `ru` |
| schema loader | `get_summary_json_schema` |
| compatibility mapper | `validate_and_shape_summary` |
| normal response format | strict `json_schema` when selected, otherwise `json_object` |
| repair response format | `json_object` |

Generic workflows use the descriptor instead of pairing a prompt, schema, and
normalizer independently.

## Canonical top-level fields

`SummaryModel` currently has 31 fields. The strict provider schema marks every
field as required and rejects additional object properties. The compatibility
mapper is intentionally more tolerant: it accepts older or incomplete payloads,
normalizes them, fills defaults, and finally validates the canonical model.

| Field | Type | Runtime behavior |
| --- | --- | --- |
| `summary_250` | string | Required after shaping; trimmed and capped at 250 characters. |
| `summary_1000` | string | Required after shaping; trimmed and capped at 1000 characters. |
| `tldr` | string | Required after shaping; trimmed and capped at 300 characters. |
| `tldr_ru` | string | Russian translation; defaults to empty, or copies a Cyrillic `tldr`. |
| `key_ideas` | string array | Empty/non-string elements are removed. |
| `topic_tags` | string array | Normalized to lowercase `#tags` and deduplicated. |
| `entities` | `Entities` | Canonical people/organizations/locations buckets. |
| `estimated_reading_time_min` | integer | Invalid input becomes `0`. |
| `key_stats` | `KeyStat[]` | Entries without a label or numeric value are dropped. |
| `answered_questions` | string array | Legacy flat answers; object input contributes answer/question text. |
| `readability` | `Readability` | Missing/invalid score is computed where possible. |
| `seo_keywords` | string array | Backfilled from extracted terms when absent. |
| `query_expansion_keywords` | string array | Shaped from supplied terms, tags, ideas, and TF-IDF; capped at 30. |
| `semantic_boosters` | string array | Standalone search text; at most 15 items, each capped at 320 characters. |
| `semantic_chunks` | `SemanticChunk[]` | Empty chunks are removed and metadata is normalized. |
| `article_id` | string or null | Supplied value, canonical URL fallback, or `null`. |
| `source_type` | enum | Invalid/missing input becomes `unknown`. |
| `temporal_freshness` | enum | Invalid/missing input becomes `unknown`. |
| `metadata` | `Metadata` | Source metadata object. |
| `extractive_quotes` | `ExtractiveQuote[]` | Entries without text are removed. |
| `highlights` | string array | Trimmed non-empty strings. |
| `questions_answered` | `QuestionAnswer[]` | Canonical question/answer pairs; supported textual forms are parsed. |
| `categories` | string array | Trimmed non-empty strings. |
| `topic_taxonomy` | `TopicTaxonomy[]` | Canonical label/score/path entries. |
| `hallucination_risk` | enum | `medium` maps to `med`; invalid/missing input becomes `unknown`. |
| `confidence` | number | Parsed and clamped to `0.0..1.0`; invalid/missing input becomes `0.0`. |
| `forwarded_post_extras` | object or null | Optional Telegram forwarded-post metadata. |
| `key_points_to_remember` | string array | Trimmed non-empty strings. |
| `insights` | `Insights` | Structured overview, facts, questions, source suggestions, and critique. |
| `quality` | `QualityAssessment` | Content-quality assessment and prompt-injection signal. |
| `summary_quality` | `SummaryQualityMetadata` | Validator/repair/extraction provenance safe to expose and persist. |

The runtime model enforces maximum lengths, not the old 150/800/50-character
minimums. Prompt wording may request richer target lengths, but those targets are
not validation constraints.

## Nested shapes

### Core analysis

```json
{
  "entities": {
    "people": [],
    "organizations": [],
    "locations": []
  },
  "readability": {
    "method": "Flesch-Kincaid",
    "score": 0.0,
    "level": "Unknown"
  },
  "key_stats": [
    {
      "label": "Latency",
      "value": 120.0,
      "unit": "ms",
      "source_excerpt": "The measured latency was 120 ms."
    }
  ]
}
```

`Entities` does not contain dates or technologies. Entity aliases and list-shaped
legacy payloads are folded into the three supported buckets and deduplicated
case-insensitively.

### Classification enums

- `source_type`: `news`, `blog`, `research`, `opinion`, `tutorial`, `reference`,
  `pdf`, `unknown`;
- `temporal_freshness`: `breaking`, `recent`, `evergreen`, `unknown`;
- `hallucination_risk`: `low`, `med`, `high`, `unknown`;
- `summary_quality.source_coverage`: `full`, `partial`, `abstract_only`,
  `transcript_missing`, `unknown`.

### Metadata and evidence

```json
{
  "metadata": {
    "title": null,
    "canonical_url": null,
    "domain": null,
    "author": null,
    "published_at": null,
    "last_updated": null
  },
  "extractive_quotes": [
    {"text": "Exact source text", "source_span": "paragraph 4"}
  ],
  "questions_answered": [
    {"question": "What changed?", "answer": "The contract changed."}
  ],
  "topic_taxonomy": [
    {"label": "software", "score": 0.9, "path": "technology/software"}
  ]
}
```

### Insights and quality

```json
{
  "insights": {
    "topic_overview": "",
    "new_facts": [
      {
        "fact": "",
        "why_it_matters": null,
        "source_hint": null,
        "confidence": null
      }
    ],
    "open_questions": [],
    "suggested_sources": [],
    "expansion_topics": [],
    "next_exploration": [],
    "caution": null,
    "critique": []
  },
  "quality": {
    "author_bias": null,
    "emotional_tone": null,
    "missing_perspectives": [],
    "evidence_quality": null,
    "prompt_injection_suspected": false
  },
  "summary_quality": {
    "validation_warnings": [],
    "repair_attempted": false,
    "repair_succeeded": false,
    "structured_output_mode": null,
    "model_used": null,
    "source_coverage": "unknown",
    "extraction_quality": null,
    "extraction_confidence": null,
    "prompt_injection_suspected": false
  }
}
```

Missing or malformed uncertainty fields deliberately receive conservative values
and corresponding `summary_quality.validation_warnings`; the normalizer never
invents a high-confidence value.

## Compatibility normalization

`validate_and_shape_summary(payload)` performs this sequence:

1. require a non-empty dictionary no larger than 100,000 characters when
   stringified;
2. map supported camelCase and legacy names to snake_case, including `summary`
   to `summary_1000`;
3. backfill the three core text fields from one another or supported evidence
   fields, then cap them;
4. normalize tags, entities, readability, evidence, classifications, and quality
   metadata;
5. derive missing search/RAG helpers;
6. validate with `SummaryModel` and return `model_dump()` output.

Compatibility aliases are an ingestion aid, not a second public schema. New code
should emit canonical snake_case field names.

## Graph enforcement and repair

The summarize graph calls the descriptor-bound structured LLM path, then its
`validate` node applies the compatibility mapper. Validation failures route to
`repair`, which appends the previous candidate and exact errors to the original
messages. The graph permits at most three repair attempts and persists every LLM
attempt. A successful validate step records repair state and inferred source
coverage in `summary_quality` before persistence.

The graph nodes are:

- `app/application/graphs/summarize/nodes/validate.py`;
- `app/application/graphs/summarize/nodes/repair.py`;
- `app/application/graphs/summarize/state.py::MAX_REPAIR_ATTEMPTS`.

## Using the contract

```python
from app.core.summary_contract import get_summary_contract_descriptor

descriptor = get_summary_contract_descriptor()
provider_format = descriptor.response_format("json_schema")
canonical = descriptor.compatibility_mapper(model_payload)
```

Do not copy the schema into an adapter. When the contract changes, update the
Pydantic model, both prompts, shaping logic, tests, persisted/API consumers, and
this reference together.

Focused validation:

```bash
uv run pytest \
  tests/test_summary_contract.py \
  tests/test_pydantic_summary.py \
  tests/test_field_normalization.py -q
```

See [Summary Contract Design](../explanation/summary-contract-design.md) for the
rationale and [SPEC](../SPEC.md#summary-json-contract-canonical) for its place in
the end-to-end system.
