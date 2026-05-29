"""Add git_mirrors table for git mirror/backup storage.

Creates the ``git_mirrors`` table and two Postgres enum types introduced by
the gitout adoption feature:

* ``git_mirrors`` -- one row per (user, clone_url) pair; tracks the clone URL,
  mirror path, health state, backoff state, and optional link to a repository
  row.

Two Postgres enums are created beforehand and dropped on downgrade:
  git_mirror_source  ('github', 'manual')
  git_mirror_status  ('pending', 'ok', 'failed', 'skipped')

Indexes
-------
- ``ix_git_mirrors_user_id``        -- on user_id (implicit FK lookup)
- ``ix_git_mirrors_user_status``    -- composite (user_id, status) for
  per-user health queries
- ``ix_git_mirrors_repository_id``  -- on repository_id for JOIN lookups

Unique constraint
-----------------
- ``uq_git_mirrors_user_clone_url`` -- (user_id, clone_url) so the same URL
  cannot be mirrored twice for the same user

Revision ID: 0030
Revises: 0029
Create Date: 2026-05-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0030"
down_revision: str = "0029"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# ---------------------------------------------------------------------------
# Reusable dialect-level enum references (create_type=False so SQLAlchemy
# never auto-emits CREATE TYPE when these appear inside op.create_table).
# The actual CREATE TYPE is emitted explicitly via DO blocks in upgrade().
# ---------------------------------------------------------------------------
_git_mirror_source = postgresql.ENUM(
    "github", "manual", name="git_mirror_source", create_type=False
)
_git_mirror_status = postgresql.ENUM(
    "pending", "ok", "failed", "skipped", name="git_mirror_status", create_type=False
)


def upgrade() -> None:
    # 1. Create the two Postgres enum types via DO blocks.
    #    - postgresql.ENUM.create(bind, checkfirst=True) is unreliable through
    #      the asyncpg sync-bridge (checkfirst query runs but CREATE still fires).
    #    - "CREATE TYPE IF NOT EXISTS" is not valid Postgres SQL syntax.
    #    - DO blocks with a pg_type catalog check are the correct portable pattern.
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'git_mirror_source') THEN
                CREATE TYPE git_mirror_source AS ENUM ('github', 'manual');
            END IF;
        END $$
    """)
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'git_mirror_status') THEN
                CREATE TYPE git_mirror_status AS ENUM ('pending', 'ok', 'failed', 'skipped');
            END IF;
        END $$
    """)

    # 2. Create the git_mirrors table.
    op.create_table(
        "git_mirrors",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("repository_id", sa.Integer(), nullable=True),
        sa.Column("source", _git_mirror_source, nullable=False),
        sa.Column("clone_url", sa.String(length=1000), nullable=False),
        sa.Column("name", sa.String(length=320), nullable=True),
        sa.Column("mirror_path", sa.String(length=1000), nullable=True),
        sa.Column("status", _git_mirror_status, nullable=False),
        sa.Column("default_branch", sa.String(length=200), nullable=True),
        sa.Column("size_kb", sa.BigInteger(), nullable=True),
        sa.Column("last_mirrored_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_error_category", sa.String(length=50), nullable=True),
        sa.Column("backoff_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("clone_strategy", sa.String(length=50), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.telegram_user_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"], ["repositories.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "clone_url", name="uq_git_mirrors_user_clone_url"),
    )

    # 3. Create indexes.
    op.create_index(
        op.f("ix_git_mirrors_user_id"), "git_mirrors", ["user_id"], unique=False
    )
    op.create_index(
        "ix_git_mirrors_user_status", "git_mirrors", ["user_id", "status"], unique=False
    )
    op.create_index(
        "ix_git_mirrors_repository_id", "git_mirrors", ["repository_id"], unique=False
    )


def downgrade() -> None:
    # 3. Drop indexes first.
    op.drop_index("ix_git_mirrors_repository_id", table_name="git_mirrors")
    op.drop_index("ix_git_mirrors_user_status", table_name="git_mirrors")
    op.drop_index(op.f("ix_git_mirrors_user_id"), table_name="git_mirrors")

    # 2. Drop the table.
    op.drop_table("git_mirrors")

    # 1. Drop the two Postgres enum types (autogenerate omits these).
    op.execute("DROP TYPE IF EXISTS git_mirror_status")
    op.execute("DROP TYPE IF EXISTS git_mirror_source")
