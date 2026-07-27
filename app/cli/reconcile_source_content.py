"""Inspect or repair Reader source-content reconciliation drift."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from app.cli._runtime import prepare_config
from app.di.database import build_runtime_database
from app.tasks.reconcile_source_content import (
    _fetch_missing_source_rows,
    _get_missing_source_stats,
    _reconcile_body,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--network-limit",
        type=int,
        default=0,
        help="Maximum network re-extractions in --apply mode (default: 0).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist repairs for one bounded batch; default is read-only.",
    )
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--db-path", type=Path, help="Deprecated; ignored.")
    parser.add_argument("--log-level")
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    if args.limit < 1 or args.limit > 10_000:
        raise ValueError("--limit must be between 1 and 10000")
    if args.network_limit < 0 or args.network_limit > args.limit:
        raise ValueError("--network-limit must be between 0 and --limit")
    cfg = prepare_config(args)
    db = build_runtime_database(cfg)
    try:
        if args.apply:
            summary = await _reconcile_body(
                cfg,
                db,
                batch_size=args.limit,
                network_limit=args.network_limit,
            )
            payload = {"dry_run": False, **asdict(summary)}
        else:
            rows = await _fetch_missing_source_rows(db, limit=args.limit)
            missing_total, oldest_missing_age_seconds = await _get_missing_source_stats(db)
            payload = {
                "dry_run": True,
                "missing_total": missing_total,
                "oldest_missing_age_seconds": oldest_missing_age_seconds,
                "sample": [
                    {
                        "summary_id": int(row["summary_id"]),
                        "request_id": int(row["request_id"]),
                        "has_local_source": bool(row["has_local_source"]),
                    }
                    for row in rows
                ],
            }
    finally:
        await db.dispose()
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
