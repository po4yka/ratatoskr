#!/usr/bin/env bash
# Run the same pip-audit input preparation as the GitHub Actions pip-audit job.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

echo "Running dependency audit with CI-equivalent filters..."

audit_file="$(mktemp "${TMPDIR:-/tmp}/ratatoskr-requirements-audit.XXXXXX")"
trap 'rm -f "$audit_file"' EXIT

# Keep this list aligned with .github/workflows/ci.yml::pip-audit-scan.
cat requirements-all.txt requirements-dev.txt | \
    grep -v "en-core-web-sm" | \
    grep -v "ru-core-news-sm" | \
    grep -v "^torch==" | \
    grep -v "^protobuf==" | \
    grep -v "^ast-serialize==" | \
    grep -v "^websockets==" | \
    sort -u > "$audit_file"

echo "Auditing $(wc -l < "$audit_file" | tr -d ' ') PyPI packages..."

: "${PIP_AUDIT_CMD:=uv run --frozen pip-audit}"
# The compiled requirements already include transitive pins, so local runs can
# avoid pip-audit's temporary resolver venv. That keeps the gate reproducible on
# uv-managed Python builds where ensurepip may be unavailable or abort.
# shellcheck disable=SC2086
$PIP_AUDIT_CMD -r "$audit_file" --strict --no-deps --disable-pip --ignore-vuln CVE-2025-3000
