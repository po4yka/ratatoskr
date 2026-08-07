#!/usr/bin/env bash
# Brings the dev stack up inside a Claude Code cloud session.
#
# Cloud environments snapshot the filesystem, not running processes: pulled
# images survive between sessions but the containers themselves do not. So the
# compose stack has to be started once per session rather than in the
# environment's setup script. Local sessions are left alone -- the operator
# owns their own compose stack there.
set -euo pipefail

# CLAUDE_CODE_REMOTE_SESSION_ID is only set inside a cloud session.
[ -n "${CLAUDE_CODE_REMOTE_SESSION_ID:-}" ] || exit 0

cd "${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}"

log="${TMPDIR:-/tmp}/ratatoskr-cloud-bootstrap.log"

if make bootstrap >"$log" 2>&1; then
  echo "Cloud dev stack ready: postgres + redis + qdrant up, migrations applied, demo data seeded."
  echo "Integration tests (make test-integration) can run in this session."
else
  echo "Cloud dev stack FAILED to start -- 'make bootstrap' exited non-zero."
  echo "Full output: $log"
  echo "Only 'make test-unit' is safe until this is resolved."
  tail -n 20 "$log"
fi
