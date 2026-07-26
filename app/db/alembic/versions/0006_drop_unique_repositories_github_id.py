"""drop unique constraint on repositories.github_id

The table-level UniqueConstraint("github_id") emitted by migration 0005
prevents two different users from starring the same GitHub repository.
The correct uniqueness boundary is the composite (user_id, github_id),
already enforced by uq_repositories_user_github.

This migration drops the redundant table-level unique constraint and
ensures the plain index ix_repositories_github_id (already created in
0005) is present for sync-lookup performance.

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-08
"""

from __future__ import annotations

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    # Drop the table-level unique constraint added by migration 0005.
    # Postgres default name for UniqueConstraint("github_id") on table
    # "repositories" is "repositories_github_id_key".
    # Guard with DO block for idempotency (e.g. already run on a DB where
    # 0005 was applied without the unique constraint).
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'repositories_github_id_key'
                  AND conrelid = 'repositories'::regclass
                  AND contype = 'u'
            ) THEN
                ALTER TABLE repositories DROP CONSTRAINT repositories_github_id_key;
            END IF;
        END $$
    """)

    # Ensure the named non-unique index exists (0005 already creates it;
    # this is a no-op on a live DB but keeps the migration self-contained).
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_repositories_github_id
        ON repositories (github_id)
    """)


def downgrade() -> None:
    # Best-effort restore of the pre-0006 table-level UNIQUE(github_id).
    #
    # That constraint is the boundary migration 0006 deliberately removed: two
    # different users may star the same GitHub repo, so uniqueness belongs on the
    # composite (user_id, github_id) (uq_repositories_user_github), not on
    # github_id alone. Recreating UNIQUE(github_id) over multi-user data would
    # raise a duplicate-key violation and abort the downgrade, so only restore it
    # when the data still permits; otherwise skip with a NOTICE (never delete
    # rows to force a constraint the schema intentionally dropped). Re-upgrading
    # is unaffected -- upgrade() drops the constraint IF EXISTS.
    op.execute("""
        DO $$
        DECLARE
            duplicate_github_ids integer;
        BEGIN
            -- Already restored (or never dropped): nothing to do.
            IF EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'repositories_github_id_key'
                  AND conrelid = 'repositories'::regclass
            ) THEN
                RETURN;
            END IF;

            SELECT count(*) INTO duplicate_github_ids
            FROM (
                SELECT 1
                FROM repositories
                WHERE github_id IS NOT NULL
                GROUP BY github_id
                HAVING count(*) > 1
            ) AS duplicates;

            IF duplicate_github_ids > 0 THEN
                RAISE NOTICE
                    'Skipping restore of repositories_github_id_key: % github_id '
                    'value(s) are starred by more than one user; the table-level '
                    'UNIQUE(github_id) cannot hold over this data. The composite '
                    'uq_repositories_user_github remains in force.',
                    duplicate_github_ids;
            ELSE
                ALTER TABLE repositories
                    ADD CONSTRAINT repositories_github_id_key UNIQUE (github_id);
            END IF;
        END $$
    """)
    # The ix_repositories_github_id index created in 0005 is kept; it
    # becomes redundant once the unique constraint is back but is harmless.
