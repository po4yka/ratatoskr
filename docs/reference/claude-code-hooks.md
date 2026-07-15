# Claude Code and Codex Hooks

Claude Code hooks live in `.claude/settings.json` for local Claude sessions. Codex uses the checked-in `.codex/hooks.json` plus scripts under `.codex/hooks/`.

The Codex hook set is the maintained repo adaptation. It preserves the useful Claude behavior, removes stale SQLite-era checks, and points prompts at the current PostgreSQL, Telethon, FastAPI, and external-web-client boundaries.

## PreToolUse Hooks

### File Protection (Write | Edit)

Blocks modifications to protected files:

- `data/ratatoskr.db` -- legacy local database path, protected because it may hold real data in old checkouts
- `requirements.txt` / `requirements-dev.txt` -- locked dependencies
- `.git/` internals

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
- Git branch and uncommitted changes

Displays quick command reference (`make format`, `make lint`, etc.).

## PostToolUse Hook (Edit | Write)

After modifying Python files, runs `ruff check --select F,E` for quick lint feedback. Shows issues immediately and suggests `make format` for auto-fix.

Skips non-Python files and files in `venv`/`build`/`dist` directories.

## UserPromptSubmit Hook

Automatically injects helpful context based on prompt keywords:

- **correlation / error id**: Preserves user-visible `Error ID: <correlation_id>` guidance and tracing tables
- **database / postgres**: Points to `app/db/session.py` and the `inspecting-database` skill
- **summary validate**: Links to `app/core/summary_contract.py`
- **firecrawl / openrouter / api**: Points to adapter files and the `debugging-apis` skill
- **test / cli**: Links to the CLI runner and `testing-workflows` skill
- **frontend / web / react / vite**: Points to the external `ratatoskr-web` source, local FastAPI `/web` serving, and the `web-frontend-dev` skill

## Customizing

Edit `.codex/hooks.json` and `.codex/hooks/*` for Codex behavior. Edit `.claude/settings.json` only for Claude Code behavior. Each hook has a `matcher` (tool name pattern) and a command script.
