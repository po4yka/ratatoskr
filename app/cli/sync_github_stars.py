"""CLI tool to manually trigger a GitHub starred-repos sync run."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

from sqlalchemy import select

from app.cli._runtime import prepare_config as _prepare_config
from app.core.logging_utils import get_logger, setup_json_logging
from app.db.models.repository import GitHubIntegrationStatus, UserGitHubIntegration
from app.di.database import build_runtime_database
from app.tasks.github_sync import _sync_all

logger = get_logger(__name__)

__all__ = ["main", "run_sync_cli"]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Trigger a GitHub starred-repos sync run locally.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--user-id",
        type=int,
        default=None,
        help="Restrict sync to this Telegram user_id; default syncs all active integrations.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Log would-be writes without committing to DB or Qdrant.",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        default=False,
        help=(
            "Force a full star snapshot instead of an incremental run: page the whole "
            "/user/starred listing and soft-unstar repos that are no longer starred. "
            "Combine with --dry-run to see what would be unstarred."
        ),
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="INFO",
        help="Override the configured log level for this session.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Path to a .env file containing environment variables for the run.",
    )
    return parser.parse_args(argv)


async def run_sync_cli(args: argparse.Namespace) -> None:
    """Execute the GitHub stars sync based on parsed CLI arguments."""
    cfg = _prepare_config(args)
    setup_json_logging(cfg.runtime.log_level)

    db = build_runtime_database(cfg, migrate=False)

    # Resolve integrations list
    async with db.session() as session:
        if args.user_id is not None:
            stmt = select(UserGitHubIntegration).where(
                UserGitHubIntegration.user_id == args.user_id,
                UserGitHubIntegration.status == GitHubIntegrationStatus.ACTIVE,
            )
        else:
            stmt = select(UserGitHubIntegration).where(
                UserGitHubIntegration.status == GitHubIntegrationStatus.ACTIVE
            )
        result = await session.execute(stmt)
        integrations: list[UserGitHubIntegration] = list(result.scalars().all())

    if not integrations:
        if args.user_id is not None:
            print(
                f"Error: No active GitHub integration for user_id={args.user_id}.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        print("no_active_integrations=true")
        return

    summary = await _sync_all(
        integrations,
        cfg=cfg,
        db=db,
        dry_run=args.dry_run,
        force_full=args.full,
    )

    try:
        import orjson

        output = orjson.dumps(asdict(summary), option=orjson.OPT_INDENT_2).decode()
    except ImportError:
        output = json.dumps(asdict(summary), indent=2, default=str)

    print(output)


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m app.cli.sync_github_stars``."""
    args = parse_args(argv)
    try:
        asyncio.run(run_sync_cli(args))
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else 1
    except KeyboardInterrupt:  # pragma: no cover - user cancelled
        return 1
    except Exception as exc:
        logger.exception("cli_sync_github_stars_failed", exc_info=exc)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
