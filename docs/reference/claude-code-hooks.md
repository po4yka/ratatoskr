# Claude Code Hooks

Hooks in `.claude/settings.json` provide automatic safety checks and environment validation for Claude Code sessions.

## PreToolUse Hooks

### File Protection (Write | Edit)

Blocks modifications to protected files:

- `data/ratatoskr.db` -- production database
- `.env` -- secrets
- `requirements.txt` / `requirements-dev.txt` -- locked dependencies

Also warns on dangerous Python patterns: `eval(`, `exec(`, `os.system`, `__import__`.

### Bash Safety

Blocks destructive shell commands:

- `rm -rf /`, `rm -rf $HOME`, `rm -rf ~`, `rm -rf /data`
- Direct disk writes (`>/dev/sd*`, `dd if=...of=/dev/`)
- Filesystem creation (`mkfs`)
- `chmod 777`
- Piping curl/wget to shell

Warns on risky operations: installing packages outside requirements, force-pushing, dropping tables, force-removing containers.

## SessionStart Hook

Runs at session start to validate the development environment:

- Python version and virtual environment status
- Core dependencies installed
- `.env` file exists with required API keys
- Database file status and size
- Git branch and uncommitted changes

Displays quick command reference (`make format`, `make lint`, etc.).

## PostToolUse Hook (Edit | Write)

After modifying Python files, runs `ruff check --select F,E` for quick lint feedback. Shows issues immediately and suggests `make format` for auto-fix.

Skips non-Python files and files in `venv`/`build`/`dist` directories.

## UserPromptSubmit Hook

Automatically injects helpful context based on prompt keywords:

- **correlation / error id**: Database query patterns for tracing
- **database / postgres**: Points to database-inspection guidance
- **summary validate**: Links to `app/core/summary_contract.py`
- **firecrawl / openrouter / api**: Points to adapter files and api-debugging skill
- **test / cli**: Links to CLI runner and telegram-testing skill
- **frontend / web / react / vite / design**: Points to `docs/reference/frontend-web.md` and the `developing-web-frontend` skill in the `ratatoskr-web` repo

## Customizing

Edit `.claude/settings.json` to modify hook behavior. Each hook has a `matcher` (tool name pattern) and a `command` (shell script) with a configurable `timeout`.
