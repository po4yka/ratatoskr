"""Database migration CLI tool.

Runs Alembic migrations to bring the PostgreSQL schema up to date.

Usage:
    # Render pending migrations as SQL without applying them
    python -m app.cli.migrate_db

    # Apply all pending migrations
    python -m app.cli.migrate_db --apply

    # Show current revision and pending migrations
    python -m app.cli.migrate_db --status [DATABASE_URL]

    # Fail unless the database is already at Alembic head
    python -m app.cli.migrate_db --check [DATABASE_URL]

    # Use the Alembic CLI directly for full control:
    alembic upgrade head
    alembic downgrade -1
    alembic history
    alembic current
    alembic stamp <revision>
"""

from __future__ import annotations

import logging
import os
import sys

from app.db.alembic_runner import (
    assert_database_at_head,
    print_status,
    render_upgrade_sql,
    upgrade_to_head,
)

logger = logging.getLogger(__name__)


def _resolve_dsn(args: list[str]) -> str:
    positional = [arg for arg in args if not arg.startswith("-")]
    return positional[0] if positional else os.getenv("DATABASE_URL", "")


def main() -> int:
    """Main entry point."""
    args = sys.argv[1:]
    show_status = "--status" in args
    apply = "--apply" in args
    check = "--check" in args
    dsn = _resolve_dsn(args)

    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )

    try:
        selected_modes = sum(1 for enabled in (show_status, apply, check) if enabled)
        if selected_modes > 1:
            logger.error("Choose only one of --status, --check, or --apply")
            return 2

        if show_status:
            print_status(dsn or None)
            return 0

        if check:
            logger.info("Checking database schema revision against Alembic head...")
            assert_database_at_head(dsn or None)
            logger.info("Database schema is at Alembic head")
            return 0

        if apply:
            logger.info("Applying database migrations via Alembic...")
            upgrade_to_head(dsn or None)
            logger.info("Database migration completed successfully")
            return 0

        logger.info("Rendering database migration SQL dry-run via Alembic...")
        render_upgrade_sql(dsn or None)
        logger.info("Database migration dry-run completed; rerun with --apply to mutate schema")
        return 0

    except Exception:
        logger.exception("Database migration failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
