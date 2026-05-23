"""CLI to manually re-trigger processing of a previously failed URL request."""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Any

from app.cli._runtime import prepare_config
from app.cli.summary import CLIMessage
from app.core.logging_utils import get_logger, setup_json_logging
from app.di.database import build_runtime_database
from app.di.repositories import build_request_repository
from app.di.shared import close_runtime_resources
from app.di.telegram import build_summary_cli_runtime

logger = get_logger(__name__)

__all__ = ["main", "run_retry_cli"]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Re-trigger processing of a failed URL request",
        allow_abbrev=False,
    )
    lookup = parser.add_mutually_exclusive_group(required=True)
    lookup.add_argument(
        "--correlation-id", metavar="CID", help="Correlation ID shown in the error message."
    )
    lookup.add_argument("--request-id", type=int, metavar="ID", help="Request primary-key ID.")
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
    parser.add_argument("--env-file", type=Path, help="Path to a .env file.")
    parser.add_argument("--json-path", type=Path, help="Write summary JSON output to file.")
    return parser.parse_args(argv)


async def run_retry_cli(args: argparse.Namespace) -> None:
    cfg = prepare_config(args)
    setup_json_logging(cfg.runtime.log_level)

    db = build_runtime_database(cfg, migrate=True)
    request_repo = build_request_repository(db)

    # Resolve the failed request.
    request: dict[str, Any] | None = None
    if args.correlation_id:
        request = await request_repo.async_get_latest_request_by_correlation_id(args.correlation_id)
        lookup_key = f"correlation_id={args.correlation_id}"
    else:
        request = await request_repo.async_get_request_by_id(args.request_id)
        lookup_key = f"request_id={args.request_id}"

    if request is None:
        print(f"cli_retry_not_found: {lookup_key}", file=sys.stderr)
        raise SystemExit(1)

    status = request.get("status", "")
    if status != "error":
        print(
            f"cli_retry_wrong_status: {lookup_key} has status='{status}', expected 'error'",
            file=sys.stderr,
        )
        raise SystemExit(1)

    input_url = request.get("input_url") or ""
    if not input_url:
        print(f"cli_retry_no_url: {lookup_key} has no input_url", file=sys.stderr)
        raise SystemExit(1)

    original_cid = request.get("correlation_id") or lookup_key
    retry_cid = f"{original_cid}-retry-1"

    logger.info(
        "cli_retry_start",
        extra={"retry_cid": retry_cid, "original_cid": original_cid, "url": input_url},
    )

    runtime = build_summary_cli_runtime(cfg, db)
    text = f"/summarize {input_url}"
    user_id: int = int(request.get("user_id") or 0)
    message = CLIMessage(text=text, json_output_path=args.json_path)
    message.from_user.id = user_id
    message.chat.id = int(request.get("chat_id") or 0)

    try:
        await runtime.command_processor.handle_summarize_command(
            message=message,
            text=text,
            uid=user_id,
            correlation_id=retry_cid,
            interaction_id=0,
            start_time=time.time(),
        )
    finally:
        await close_runtime_resources(
            runtime.url_processor,
            runtime.search.vector_store,
            runtime.search.embedding_service,
            runtime.core.firecrawl_client,
            runtime.core.llm_client,
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        asyncio.run(run_retry_cli(args))
    except KeyboardInterrupt:  # pragma: no cover
        return 1
    except SystemExit:
        return 1
    except Exception as exc:
        logger.exception("cli_retry_failed", exc_info=exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
