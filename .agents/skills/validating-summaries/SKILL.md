---
name: validating-summaries
description: >
  Validate summary JSON contracts against strict schema requirements.
  Trigger keywords: summary validation, summary contract, JSON schema,
  character limits, summary fields, validate summary.
version: 2.0.1
allowed-tools: Bash, Read
---

# Validating Summaries

Validates summary JSON output against the strict contract defined in
`app/core/summary_contract.py`.

## Summary JSON Contract

See `app/core/summary_schema.py` for the full Pydantic model.

## Validation Rules (overview)

| Rule | Constraint |
|------|-----------|
| `summary_250` | Hard cap 250 chars, sentence boundary |
| `summary_1000` | Hard cap 1000 chars, multi-sentence |
| `topic_tags` | Leading `#`, deduplicated, max 10 recommended |
| `entities` | Lists deduplicated case-insensitively; categories: people, organizations, locations |
| `key_stats` | `value` numeric, `label` + `source_excerpt` required, `unit` optional |
| `readability` | `method` string, `score` numeric, `level` mapped from score |

Full details with code fix snippets: `references/validation-rules.md`

## Validation Scripts

### Standalone validator

Checks required fields, character limits, tag format, and entity structure:

```bash
python .codex/skills/validating-summaries/scripts/validate-summary.py summary.json
```

### Project-integrated validator

Uses `validate_summary_json()` from the project:

```bash
python .codex/skills/validating-summaries/scripts/validate-with-project.py summary.json
```

## Testing with CLI Runner

Test URL processing and summary generation end-to-end:

```bash
python -m app.cli.summary \
  --url https://example.com/article \
  --json-path output.json \
  --log-level DEBUG
```

The CLI automatically validates summaries using `validate_summary_json()`.

## Reference Files

- **Contract validation**: `app/core/summary_contract.py`
- **Schema definition**: `app/core/summary_schema.py`
- **LLM prompts**: `app/prompts/summary_system_en.txt`, `app/prompts/summary_system_ru.txt`
- **JSON utilities**: `app/core/json_utils.py` (includes repair logic)
- **Validation rules**: `references/validation-rules.md`
- **Standalone script**: `.codex/skills/validating-summaries/scripts/validate-summary.py`
- **Project script**: `.codex/skills/validating-summaries/scripts/validate-with-project.py`

## Important Notes

- All validation happens in `app/core/summary_contract.py`
- JSON repair attempts to fix malformed LLM output (`json_repair` library)
- Both English and Russian prompts must be kept in sync
- Database stores verbatim JSON in `summaries.json_payload`
- Failed validations are logged with correlation ID for debugging
