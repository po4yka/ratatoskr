# Validation Rules

Detailed validation rules for the summary JSON contract.

## Character Limits

- `summary_250`: HARD CAP at 250 characters, must end on sentence/phrase boundary
- `summary_1000`: HARD CAP at 1000 characters, multi-sentence overview

## Topic Tags

- Must have leading `#` character
- Deduplicated (case-sensitive)
- Maximum 10 tags recommended

## Entities

- Lists must be deduplicated (case-insensitive)
- Valid categories: `people`, `organizations`, `locations`

## Key Stats

- `value` must be numeric (int or float)
- `label` and `source_excerpt` are required strings
- `unit` is optional string

## Readability

- `method` typically "Flesch-Kincaid" or "Flesch Reading Ease"
- `score` is numeric
- `level` maps score to reading level (e.g., "College", "High School")

---

## Common Validation Issues

### 1. Character Limit Exceeded

**Problem**: `summary_250` or `summary_1000` too long

**Solution**: Truncate at sentence boundary

```python
def truncate_at_sentence(text, max_len):
    if len(text) <= max_len:
        return text
    # Find last sentence ending before limit
    for end in ['. ', '! ', '? ']:
        pos = text[:max_len].rfind(end)
        if pos > 0:
            return text[:pos+1]
    return text[:max_len]
```

### 2. Missing Tag Hashtags

**Problem**: Topic tags without leading `#`

**Solution**: Prefix all tags

```python
tags = [f"#{tag}" if not tag.startswith('#') else tag for tag in tags]
```

### 3. Duplicate Entities

**Problem**: Case-insensitive duplicates in entity lists

**Solution**: Deduplicate preserving case

```python
def dedupe_entities(items):
    seen = set()
    result = []
    for item in items:
        if item.lower() not in seen:
            seen.add(item.lower())
            result.append(item)
    return result
```

### 4. Invalid Key Stats Format

**Problem**: Missing required fields or wrong types

**Solution**: Validate each stat

```python
for stat in key_stats:
    assert 'label' in stat, "Missing label"
    assert 'value' in stat, "Missing value"
    assert isinstance(stat['value'], (int, float)), "Value must be numeric"
```
