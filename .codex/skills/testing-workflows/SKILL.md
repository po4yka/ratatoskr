---
name: testing-workflows
description: Test bot functionality locally using CLI runner, message simulation, and workflow validation. Trigger on "test", "CLI runner", "message simulation", "bot testing", "workflow validation", "pytest".
version: 2.0.1
allowed-tools: Bash, Read, Write
---

# Testing Workflows Skill

Test bot functionality locally using CLI tools, message simulation, and unit tests.

## CLI Summary Runner

The CLI runner tests the full URL processing pipeline without Telegram credentials.
Telegram credentials (`API_ID`, `API_HASH`, `BOT_TOKEN`) are NOT required -- the CLI generates stub credentials automatically.

### Basic Usage

```bash
# Summarize a single URL
python -m app.cli.summary --url https://example.com/article

# With custom output and debug logging
python -m app.cli.summary \
  --url https://example.com/article \
  --json-path summary.json \
  --log-level DEBUG

# Auto-accept multiple URLs
python -m app.cli.summary \
  --url "https://example.com/1 https://example.com/2" \
  --accept-multiple

# Simulate message text (like Telegram input)
python -m app.cli.summary "/summarize https://example.com/article"
```

### CLI Features

- **Full pipeline**: URL normalization -> scraper chain -> LLM -> JSON validation
- **Deduplication**: Respects `dedupe_hash` (won't re-crawl same URL)
- **Insights generation**: Optional advanced analysis with retry logic
- **JSON repair**: Handles malformed LLM output
- **Correlation IDs**: Generates unique IDs for tracing

## Testing Specific Workflows

Standalone test scripts are bundled with this skill in `./scripts/`. Run them directly:

```bash
python .codex/skills/testing-workflows/scripts/test-url-normalization.py
python .codex/skills/testing-workflows/scripts/test-summary-validation.py
python .codex/skills/testing-workflows/scripts/test-language-detection.py
python .codex/skills/testing-workflows/scripts/test-access-control.py
```

## Simulating Telegram Messages

### Message Models

See `app/models/telegram/telegram_message.py` for data structures:

```python
from app.models.telegram.telegram_message import TelegramMessage
from app.models.telegram.telegram_chat import TelegramChat

test_message = TelegramMessage(
    message_id=12345,
    date=1234567890,
    chat=TelegramChat(id=111, type="private"),
    text="https://example.com/article",
    from_user={"id": 123456789, "username": "testuser"}
)
```

### Test Message Router

For routing logic tests, instantiate `MessageRouter` with mocked dependencies and call it directly. See `app/adapters/telegram/message_router.py` for the constructor signature and existing router tests under `tests/adapters/telegram/` for setup patterns.

## End-to-End URL Flow

```bash
# Run full pipeline
python -m app.cli.summary \
  --url https://example.com/article \
  --json-path test_output.json \
  --log-level DEBUG

# Verify output
cat test_output.json | python -m json.tool

# Verify in database
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT id, type, status, input_url
     FROM requests
    ORDER BY created_at DESC
    LIMIT 1;"
```

### Error Handling

```bash
# Test with invalid URL
python -m app.cli.summary --url "not-a-url"

# Test with unreachable URL
python -m app.cli.summary --url "https://thisurldoesnotexist12345.com"

# Check errors in database
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT id, status, input_url
     FROM requests
    WHERE status = 'error'
    ORDER BY created_at DESC
    LIMIT 5;"
```

## Running pytest

```bash
# Run all tests
python -m pytest tests/ -v

# Run a single test file (prefer this for faster iteration)
python -m pytest tests/test_url_utils.py -v

# Run with coverage
python -m pytest tests/ --cov=app --cov-report=html
```

## References

- `references/bot-commands.md` -- Available bot commands and processing flow
- `references/e2e-testing.md` -- Docker testing and E2E test setup

## Key Files

- **CLI Runner**: `app/cli/summary.py`
- **Message Router**: `app/adapters/telegram/message_router.py`
- **Access Controller**: `app/adapters/telegram/access_controller.py`
- **URL Handler**: `app/adapters/telegram/url_handler.py`
- **Command Processor**: `app/adapters/telegram/command_processor.py`

## Important Notes

- CLI runner does NOT send actual Telegram messages
- Database operations work exactly like production
- All validation logic is the same as the live bot
- Use correlation IDs to trace requests across logs and DB
- Test both English and Russian language flows
