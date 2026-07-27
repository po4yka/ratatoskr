"""Dry-run the Reader source-content reconciliation scan."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from app.cli._runtime import prepare_config
from app.di.database import build_runtime_database
from app.tasks.reconcile_source_content import _fetch_missing_source_rows


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--db-path", type=Path, help="Deprecated; ignored.")
    parser.add_argument("--log-level")
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    if args.limit < 1 or args.limit > 10_000:
        raise ValueError("--limit must be between 1 and 10000")
    cfg = prepare_config(args)
    db = build_runtime_database(cfg)
    try:
        rows = await _fetch_missing_source_rows(db, limit=args.limit)
    finally:
        await db.dispose()
    payload = {
        "dry_run": True,
        "missing_total": int(rows[0]["missing_total"]) if rows else 0,
        "sample": [
            {
                "summary_id": int(row["summary_id"]),
                "request_id": int(row["request_id"]),
                "has_local_source": bool(row["has_local_source"]),
            }
            for row in rows
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
