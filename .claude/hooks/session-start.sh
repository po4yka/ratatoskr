#!/usr/bin/env bash
set -euo pipefail

cd "${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}"

echo "=== Ratatoskr Claude Session Started ==="
echo

python_bin="python3"
if [ -x ".venv/bin/python" ]; then
  python_bin=".venv/bin/python"
fi

if command -v "$python_bin" >/dev/null 2>&1; then
  echo "Python: $($python_bin --version 2>&1 | awk '{print $2}') ($python_bin)"
else
  echo "Python 3 not found"
fi

if [ -n "${VIRTUAL_ENV:-}" ]; then
  echo "Virtual environment: active ($VIRTUAL_ENV)"
elif [ -d ".venv" ]; then
  echo "Virtual environment: .venv available (hooks use it automatically)"
else
  echo "Virtual environment: missing"
fi

if "$python_bin" -c "import telethon, fastapi, sqlalchemy, asyncpg" >/dev/null 2>&1; then
  echo "Core dependencies: installed"
else
  echo "Core dependencies: missing or incomplete"
fi

if [ -f ".env" ]; then
  echo "Environment: .env exists (values not inspected)"
else
  echo "Environment: .env not found"
fi

if git rev-parse --git-dir >/dev/null 2>&1; then
  echo "Git branch: $(git branch --show-current)"
  if ! git diff-index --quiet HEAD -- 2>/dev/null; then
    echo "Git: uncommitted changes detected"
  fi
fi

echo
echo "Quick commands:"
echo "  make format"
echo "  make lint"
echo "  make type"
echo "  python -m app.cli.summary --url <URL>"
echo "  make stage-web WEB_REPO=../ratatoskr-web"
echo
