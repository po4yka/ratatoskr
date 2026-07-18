#!/usr/bin/env bash
set -euo pipefail

cd "${CODEX_PROJECT_DIR:-$(git rev-parse --show-toplevel)}"

echo "=== Ratatoskr Codex Session Started ==="
echo

python_command=""
if [ -x ".venv/bin/python" ]; then
  python_command=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  python_command="$(command -v python3)"
fi

if [ -n "$python_command" ]; then
  python_version="$($python_command --version 2>&1 | awk '{print $2}')"
  echo "Python: $python_version"
else
  echo "Python 3 not found"
fi

if [ -n "${VIRTUAL_ENV:-}" ]; then
  echo "Virtual environment: active ($VIRTUAL_ENV)"
elif [ -d ".venv" ]; then
  echo "Virtual environment: .venv exists but is not activated"
else
  echo "Virtual environment: missing"
fi

if [ -n "$python_command" ] && "$python_command" -c "import telethon, fastapi, sqlalchemy, asyncpg" >/dev/null 2>&1; then
  echo "Core dependencies: installed"
else
  echo "Core dependencies: missing or incomplete"
fi

if [ -f ".env" ]; then
  echo "Environment: .env exists (contents not inspected)"
else
  echo "Environment: .env not found"
fi

if git rev-parse --git-dir >/dev/null 2>&1; then
  branch="$(git branch --show-current)"
  echo "Git branch: $branch"
  if ! git diff-index --quiet HEAD -- 2>/dev/null; then
    echo "Git: uncommitted changes detected"
  fi
fi

echo
echo "Quick commands:"
echo "  make format"
echo "  make lint"
echo "  make type"
echo "  .venv/bin/python -m app.cli.summary --url <URL>"
echo
