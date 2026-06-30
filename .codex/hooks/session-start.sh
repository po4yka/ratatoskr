#!/usr/bin/env bash
set -euo pipefail

cd "${CODEX_PROJECT_DIR:-$(git rev-parse --show-toplevel)}"

echo "=== Ratatoskr Codex Session Started ==="
echo

if command -v python3 >/dev/null 2>&1; then
  python_version="$(python3 --version 2>&1 | awk '{print $2}')"
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

if python3 -c "import telethon, fastapi, sqlalchemy, asyncpg" >/dev/null 2>&1; then
  echo "Core dependencies: installed"
else
  echo "Core dependencies: missing or incomplete"
fi

if [ -f ".env" ]; then
  required_keys=("OPENROUTER_API_KEY" "BOT_TOKEN" "DATABASE_URL")
  missing_keys=()
  for key in "${required_keys[@]}"; do
    if ! grep -q "^${key}=" .env 2>/dev/null; then
      missing_keys+=("$key")
    fi
  done
  if [ "${#missing_keys[@]}" -eq 0 ]; then
    echo "Environment: .env exists with required keys"
  else
    echo "Environment: .env missing keys: ${missing_keys[*]}"
  fi
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
echo "  python -m app.cli.summary --url <URL>"
echo
