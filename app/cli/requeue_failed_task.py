"""CLI helper to re-enqueue Taskiq failed jobs from the dead-letter table."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import sys
from pathlib import Path
from typing import Any

from app.cli._runtime import prepare_config
from app.core.logging_utils import get_logger, setup_json_logging
from app.di.database import build_runtime_database
from app.infrastructure.persistence.repositories.taskiq_failed_job_repository import (
    TaskiqFailedJobRepository,
)

logger = get_logger(__name__)

_TASK_MODULES = (
    "app.tasks.digest",
    "app.tasks.git_backup_sync",
    "app.tasks.github_sync",
    "app.tasks.import_tasks",
    "app.tasks.langgraph_prune",
    "app.tasks.purge_raw_data",
    "app.tasks.reconcile_vector_index",
    "app.tasks.rss",
    "app.tasks.url_processing",
    "app.tasks.x_bookmarks_sync",
    "app.tasks.x_wiki_sync",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Re-enqueue one Taskiq dead-lettered job by ID.",
        allow_abbrev=False,
    )
    parser.add_argument("failed_job_id", type=int, metavar="ID")
    parser.add_argument("--env-file", type=Path, help="Path to a .env file.")
    parser.add_argument(
        "--db-path",
        type=Path,
        help="Deprecated; ignored by the PostgreSQL-backed runtime.",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Override log level.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the failed-job row and task registration without enqueueing.",
    )
    return parser.parse_args(argv)


async def run_requeue_failed_task_cli(args: argparse.Namespace) -> None:
    cfg = prepare_config(args)
    setup_json_logging(cfg.runtime.log_level)
    _import_task_modules()

    from app.tasks.broker import broker

    db = build_runtime_database(cfg)
    repo = TaskiqFailedJobRepository(db)
    try:
        row = await repo.async_get_failed_job(args.failed_job_id)
        if row is None:
            print(f"taskiq_failed_job_not_found: id={args.failed_job_id}", file=sys.stderr)
            raise SystemExit(1)
        if row.get("status") != "dead_letter":
            print(
                f"taskiq_failed_job_not_requeueable: id={args.failed_job_id} status={row.get('status')}",
                file=sys.stderr,
            )
            raise SystemExit(1)

        task_name = str(row["task_name"])
        task = broker.find_task(task_name)
        if task is None:
            print(f"taskiq_task_not_registered: {task_name}", file=sys.stderr)
            raise SystemExit(1)

        args_payload = _as_list(row.get("args_json"))
        kwargs_payload = _as_dict(row.get("kwargs_json"))
        labels = _requeue_labels(_as_dict(row.get("labels_json")))

        if args.dry_run:
            print(f"taskiq_failed_job_requeue_dry_run: id={args.failed_job_id} task={task_name}")
            return

        await task.kicker().with_labels(**labels).kiq(*args_payload, **kwargs_payload)
        await repo.async_mark_requeued(args.failed_job_id)
        print(f"taskiq_failed_job_requeued: id={args.failed_job_id} task={task_name}")
    finally:
        await db.dispose()


def _import_task_modules() -> None:
    for module_name in _TASK_MODULES:
        importlib.import_module(module_name)


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _requeue_labels(labels: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(labels)
    cleaned.pop("_retries", None)
    return cleaned


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        asyncio.run(run_requeue_failed_task_cli(args))
    except KeyboardInterrupt:  # pragma: no cover
        return 1
    except SystemExit:
        return 1
    except Exception as exc:
        logger.exception("taskiq_failed_job_requeue_failed", exc_info=exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
