#!/usr/bin/env bash
# Full Alembic downgrade/upgrade round-trip smoke test.
#
# The historical CI check only stepped a single Alembic revision down on an
# empty database, which exercised exactly one of the ~50 downgrade() functions
# and never touched real rows. That let two classes of bug reach production
# rollbacks undetected:
#
#   1. Structural bugs anywhere in the chain -- e.g. two migrations that both
#      create the same index, so the downgrade path drops it twice and the
#      second, non-idempotent drop raises UndefinedObject.
#   2. Data-dependent bugs -- e.g. a downgrade that recreates a table-level
#      UNIQUE constraint which existing rows violate (the 0006 duplicate
#      github_id case; CLAUDE.md Operating Rule #12).
#
# This script exercises EVERY downgrade() (head -> base) against a database
# seeded with a representative dataset, then re-upgrades to head, so both classes
# surface in CI instead of during a real rollback.
#
# Requires DATABASE_URL to point at a disposable Postgres.
set -euo pipefail

: "${DATABASE_URL:?DATABASE_URL is required}"

echo "== Applying all migrations to head =="
python -m app.cli.migrate_db --apply

echo "== Seeding representative data (incl. the 0006 duplicate-github_id case) =="
python -m tools.scripts.seed_migration_roundtrip

echo "== Downgrading to base (every downgrade() runs, with data present) =="
alembic downgrade base

echo "== Re-upgrading to head =="
alembic upgrade head

echo "== Verifying database is at head =="
alembic current 2>&1 | grep -q "(head)"

echo "Migration round-trip succeeded"
