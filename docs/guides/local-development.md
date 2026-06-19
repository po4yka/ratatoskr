# Local Development

Set up a local development environment for Ratatoskr.

**Time**: ~20 minutes **Difficulty**: Intermediate **Prerequisites**: Python 3.13+, git, Node.js 20+ (for web frontend work)

---

## What You'll Learn

By the end of this tutorial, you'll have:

- ✅ Local development environment with Python venv
- ✅ All dependencies installed (including dev tools)
- ✅ Pre-commit hooks configured
- ✅ Tests running successfully
- ✅ web interface running locally
- ✅ CLI summary runner working
- ✅ Ready to make your first code change

---

## Step 1: Clone Repository (1 minute)

```bash
# Clone the repository
git clone https://github.com/po4yka/ratatoskr.git
cd ratatoskr

# Verify Python version (3.13+ required)
python3 --version
# Should output: Python 3.13.x or higher
```

### Fast Path: Bootstrap Services and Demo Data

For backend/API work that only needs a local database, Redis, Qdrant, and non-empty library data, use the one-command bootstrap:

```bash
make bootstrap
```

This runs `uv sync --all-extras --dev`, installs pre-commit hooks, starts Postgres + Redis + Qdrant through `ops/docker/docker-compose.yml` plus `ops/docker/docker-compose.dev.yml`, waits for health checks, applies Alembic migrations, and seeds demo summaries for `ALLOWED_USER_IDS=424242`.

Useful follow-up commands:

```bash
make seed-demo-data                 # refresh the demo user and sample summaries
make teardown-dev                   # stop local services and remove dev volumes
DEV_USER_ID=123456 make bootstrap   # use a different allowlisted demo user
```

Local endpoints after bootstrap:

- PostgreSQL: `postgresql+asyncpg://ratatoskr_app:ratatoskr-dev-password@127.0.0.1:5432/ratatoskr`
- Redis: `redis://127.0.0.1:6379/0`
- Qdrant: `http://127.0.0.1:6333`
- API after starting it: `POSTGRES_PASSWORD=ratatoskr-dev-password ALLOWED_USER_IDS=424242 docker compose -f ops/docker/docker-compose.yml -f ops/docker/docker-compose.dev.yml up -d mobile-api`, then open `http://127.0.0.1:18000`
- Grafana after starting monitoring: `POSTGRES_PASSWORD=ratatoskr-dev-password COMPOSE_PROFILES=with-monitoring docker compose -f ops/docker/docker-compose.yml -f ops/docker/docker-compose.dev.yml up -d grafana`, then open `http://127.0.0.1:3001`

**If Python 3.13 not installed**:

```bash
# Using pyenv (recommended)
pyenv install 3.13.0
pyenv local 3.13.0

# Verify
python3 --version
```

---

## Step 2: Create Virtual Environment (2 minutes)

```bash
# Create virtual environment
python3 -m venv .venv

# Activate virtual environment
source .venv/bin/activate

# Your shell prompt should now show (.venv)

# Upgrade pip
pip install --upgrade pip
```

**macOS/Linux alternative**:

```bash
# Use the provided script
make venv
source .venv/bin/activate
```

**Windows (PowerShell)**:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

---

## Step 3: Install Dependencies (3 minutes)

```bash
# Install production dependencies
pip install -r requirements.txt

# Install development dependencies
pip install -r requirements-dev.txt

# Verify installation
pip list | grep -E "telethon | firecrawl | ruff |pytest"

# Should see:
# firecrawl          x.x.x
# telethon           x.x.x
# pytest             x.x.x
# ruff               x.x.x
```

**Common Issues**:

- **ARM Mac (M1/M2) compilation errors**: Install build tools

  ```bash
  brew install cmake pkg-config
  pip install -r requirements.txt
  ```

- **Linux missing system libraries**:

  ```bash
  sudo apt-get install build-essential python3-dev
  pip install -r requirements.txt
  ```

---

## Step 4: Configure Environment (2 minutes)

```bash
# Copy example environment file
cp .env.example .env

# Edit .env with your API keys
nano .env  # or vim, code, etc.
```

**Minimal configuration for local development**:

```bash
# Telegram (required)
API_ID=your_api_id
API_HASH=your_api_hash
BOT_TOKEN=your_bot_token
ALLOWED_USER_IDS=your_user_id

# LLM (required)
OPENROUTER_API_KEY=your_openrouter_key
OPENROUTER_MODEL=deepseek/deepseek-v4-flash

# Database (local dev)
DB_PATH=./data/ratatoskr.db

# Logging
LOG_LEVEL=DEBUG
```

Content extraction uses the built-in multi-provider chain (Scrapling → Crawl4AI → Firecrawl → Defuddle → Playwright → Crawlee → direct HTML → Scrapegraph-AI) with no API key required for the default in-process providers. See [`docs/explanation/scraper-chain.md`](../explanation/scraper-chain.md) for the full chain reference.

**Get API keys**: See [Quickstart Tutorial § Get API Keys](quickstart.md#step-1-get-api-keys-3-minutes)

---

## Step 5: Initialize Database (1 minute)

```bash
# Create data directory
mkdir -p data

# Run database migrations
python -m app.cli.migrate_db

# Verify database created
ls -lh data/
# Should see Compose-managed Postgres volume (`postgres_data/`) in `docker volume inspect`

# Check database schema (lists tables and indexes)
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c "\dt"
```

---

## Step 6: Install Pre-commit Hooks (2 minutes)

Pre-commit hooks ensure code quality before commits.

```bash
# Install pre-commit
pip install pre-commit

# Install git hooks
pre-commit install

# Test hooks manually
pre-commit run --all-files

# Should run: ruff (check + format), isort, mypy, trailing-whitespace, etc.
```

**What pre-commit does**:

- **Ruff**: Auto-fixes code style issues
- **isort**: Sorts imports (black-compatible)
- **mypy**: Type checking
- **Standard hooks**: Trailing whitespace, YAML syntax, merge conflicts

**First run** will download hook environments (~2 minutes).

---

## Step 7: Run Tests (3 minutes)

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=app --cov-report=term-missing

# Run specific test file
pytest tests/test_url_utils.py

# Run tests matching pattern
pytest -k "test_normalize_url"

# Run with verbose output
pytest -v

# Run fast (skip slow tests)
pytest -m "not slow"
```

**Expected output**:

```
============================= test session starts ==============================
collected 150 items

tests/test_access_control.py ........                                    [  5%]
tests/test_json_repair.py .........                                      [ 11%]
tests/test_url_utils.py .................                                [ 22%]
...
tests/test_summary_contract.py ......................                    [100%]

============================== 150 passed in 12.34s ============================
```

**Note**: Some tests may fail due to missing `adaptive_timeout` field in test config. These are pre-existing failures documented in `MEMORY.md`.

---

## Step 7.5: Run Web Interface (Optional, 3 minutes)

The web frontend lives in the separate **ratatoskr-web** repository. Clone it alongside this repo and follow its `CLAUDE.md` / `docs/reference/frontend-web.md` for the local dev workflow.

```bash
# From the ratatoskr-web repo root
npm ci
npm run dev       # Vite dev server at http://localhost:5173
```

Set `VITE_API_BASE_URL=http://localhost:8000` in `.env` so the dev server proxies to the local API.

Useful URLs during local development:

- Web app (Vite): `http://localhost:5173`
- API host (when running `uvicorn app.api.main:app --reload`): `http://localhost:8000`
- Same-host SPA route (when web bundle is deployed to `app/static/web/`): `http://localhost:8000/web/library`

---

## Step 8: Use CLI Summary Runner (2 minutes)

Test URL processing without running the full bot.

```bash
# Summarize a URL
python -m app.cli.summary --url https://example.com/article

# Expected output:
# INFO: Extracting content from https://example.com/article
# INFO: Generating summary...
# INFO: Summary generated successfully
# [JSON output printed to console]

# Save summary to file
python -m app.cli.summary --url https://example.com/article --json-path summary.json

# Process multiple URLs
python -m app.cli.summary --url https://example.com/article1 --url https://example.com/article2 --accept-multiple

# Verbose logging
python -m app.cli.summary --url https://example.com/article --log-level DEBUG

# Mimic Telegram command
python -m app.cli.summary "/summarize https://example.com/article"
```

**CLI advantages**:

- Fast iteration (no bot startup)
- No Telegram credentials needed (CLI generates stubs)
- Easy debugging (verbose logs, JSON output)
- Scriptable (batch processing)

---

## Step 9: Make Your First Code Change (5 minutes)

Let's make a small change to verify your setup works end-to-end.

### 9.1 Create a Feature Branch

```bash
git checkout -b feature/test-change
```

### 9.2 Make a Small Change

Edit `app/core/url_utils.py` and add a comment:

```python
def normalize_url(url: str) -> str:
    """
    Normalize URL for deduplication.

    # Test change: This function is awesome!

    Args:
        url: Raw URL from user
    ...
```

### 9.3 Run Pre-commit Checks

```bash
# Pre-commit runs automatically on commit, but you can test manually
pre-commit run --all-files

# Should pass (ruff, isort, mypy all happy)
```

### 9.4 Run Tests

```bash
# Verify your change didn't break anything
pytest tests/test_url_utils.py

# All tests should still pass
```

### 9.5 Commit Your Change

```bash
git add app/core/url_utils.py
git commit -m "docs: add comment to normalize_url function"

# Pre-commit hooks run automatically
# If they modify files, stage changes and commit again
```

---

## Step 10: Run the Bot Locally (Optional)

If you want to test the full bot:

```bash
# Ensure .env has all required variables
# Then run the bot
python bot.py

# Expected output:
# INFO: Bot started successfully
# INFO: Listening for messages...

# Test by messaging your bot on Telegram
# Send a URL to get a summary
```

**Stop the bot**: Press `Ctrl+C`

---

## Development Workflow

### Daily Workflow

```bash
# 1. Activate venv
source .venv/bin/activate

# 2. Pull latest changes
git pull origin main

# 3. Install any new dependencies
pip install -r requirements.txt -r requirements-dev.txt

# 4. Create feature branch
git checkout -b feature/my-feature

# 5. Make changes
# ... edit code ...

# 6. Run tests
pytest

# 7. Commit (pre-commit runs automatically)
git commit -m "feat: implement my feature"

# 8. Push and create PR
git push origin feature/my-feature
```

### Code Quality Commands

```bash
# Format code (ruff + isort)
make format

# Lint code
make lint

# Type check
make type

# Run all quality checks
make format lint type

# Web static checks (run from ratatoskr-web repo)
# npm run check:static
```

### Debugging Tips

```bash
# Enable debug logging
export LOG_LEVEL=DEBUG

# Enable bounded API payload previews; tokens, prompts, raw content, and private URLs stay redacted
export DEBUG_PAYLOADS=1

# Run CLI with verbose output
python -m app.cli.summary --url https://example.com --log-level DEBUG

# Inspect database (interactive psql shell)
docker exec -it ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr

# Check specific request by correlation ID
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT * FROM requests WHERE correlation_id = '<correlation_id>';"
```

---

## Common Development Tasks

### Adding a New Dependency

```bash
# 1. Add to pyproject.toml [project.dependencies]
# 2. Lock dependencies
make lock-uv

# 3. Install
pip install -r requirements.txt

# 4. Commit both pyproject.toml and requirements.txt
git add pyproject.toml requirements.txt
git commit -m "deps: add new-package"
```

### Running Database Migrations

```bash
# Apply migrations (Alembic upgrade head)
python -m app.cli.migrate_db

# Inspect current Alembic revision
python -m app.cli.migrate_db --status

# Verify database connectivity
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c "SELECT 1;"
```

### Testing Mobile API

```bash
# Start API server
uvicorn app.api.main:app --reload

# In another terminal, test endpoints
curl http://localhost:8000/health

# Or use the OpenAPI docs
open http://localhost:8000/docs
```

### Testing Web Routes Through FastAPI

```bash
# Build web bundle in ratatoskr-web, then copy dist/ into app/static/web/
# (from ratatoskr-web repo):  npm run build && cp -r dist/ ../ratatoskr/app/static/web/

# Start API host
uvicorn app.api.main:app --reload

# Open web app served by FastAPI
open http://localhost:8000/web/library
```

### Testing MCP Server

```bash
# Start MCP server
python -m app.cli.mcp_server

# Start MCP SSE transport safely (loopback + scoped user)
python -m app.cli.mcp_server --transport sse --user-id 123456789
```

---

## IDE Setup

### VS Code

Recommended extensions:

- **Python** (ms-python.python)
- **Pylance** (ms-python.vscode-pylance)
- **Ruff** (charliermarsh.ruff)
- **isort** (ms-python.isort)

`.vscode/settings.json`:

```json
{
  "python.defaultInterpreterPath": "${workspaceFolder}/.venv/bin/python",
  "python.linting.enabled": true,
  "python.linting.ruffEnabled": true,
  "python.formatting.provider": "none",
  "[python]": {
    "editor.formatOnSave": true,
    "editor.codeActionsOnSave": {
      "source.fixAll": true,
      "source.organizeImports": true
    },
    "editor.defaultFormatter": "charliermarsh.ruff"
  }
}
```

### PyCharm

1. **Set Interpreter**: Settings → Project → Python Interpreter → Add → Virtualenv → Existing → `.venv/bin/python`
2. **Enable Ruff**: Settings → Tools → External Tools → Add Ruff
3. **Configure pytest**: Settings → Tools → Python Integrated Tools → Testing → pytest

---

## Troubleshooting

### Virtual Environment Not Activating

```bash
# macOS/Linux
source .venv/bin/activate

# Windows PowerShell
.venv\Scripts\Activate.ps1

# Windows CMD
.venv\Scripts\activate.bat

# If issues persist, recreate venv
rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

### Pre-commit Hooks Failing

```bash
# Update hooks
pre-commit autoupdate

# Clear cache and reinstall
pre-commit clean
pre-commit install

# Skip hooks temporarily (NOT recommended)
git commit --no-verify
```

### Tests Failing

```bash
# Check if failures are pre-existing
git stash
pytest  # Run tests on clean main branch
git stash pop

# If failures match, they're pre-existing (see MEMORY.md)
# If new failures, debug with:
pytest -v tests/test_failing.py
pytest --pdb  # Drop into debugger on failure
```

### Import Errors

```bash
# Ensure dependencies installed
pip install -r requirements.txt -r requirements-dev.txt

# Check PYTHONPATH
echo $PYTHONPATH

# Add project root to PYTHONPATH if needed
export PYTHONPATH="${PYTHONPATH}:$(pwd)"
```

---

## Next Steps

**You're ready to develop!** 🎉

**Explore the codebase**:

- Read [CLAUDE.md](../../CLAUDE.md) - Comprehensive codebase guide
- Read [SPEC.md](../SPEC.md) - Technical specification

**Make contributions**:

- Fix bugs or add features
- Improve documentation
- Add tests

**Get help**:

- [TROUBLESHOOTING.md](../reference/troubleshooting.md) - Debugging guide
- [FAQ](../explanation/faq.md) - Common questions
- [GitHub Issues](https://github.com/po4yka/ratatoskr/issues) - Ask questions

---

**Tutorial Complete!** 🎓

You now have a fully functional local development environment. Happy coding!

---

**Last Updated**: 2026-03-28
